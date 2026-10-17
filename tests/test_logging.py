import logging
from datetime import datetime, timedelta
from pathlib import Path

from grok_search import logger as logger_module
from grok_search.logger import DailyFileHandler


def _record(message: str, when: datetime) -> logging.LogRecord:
    record = logging.LogRecord("grok_search", logging.ERROR, __file__, 1, message, None, None)
    record.created = when.timestamp()
    return record


def test_the_package_logger_uses_the_daily_handler():
    assert any(isinstance(h, DailyFileHandler) for h in logger_module.logger.handlers)


def test_records_go_to_the_file_of_their_own_day(tmp_path):
    handler = DailyFileHandler(tmp_path, retention=7)
    handler.setFormatter(logging.Formatter("%(message)s"))
    now = datetime.now()
    later = now + timedelta(days=3)
    try:
        handler.handle(_record("today", now))
        handler.handle(_record("three days later", later))
        handler.handle(_record("still later", later))
    finally:
        handler.close()
    today_file = tmp_path / f"grok_search_{now:%Y%m%d}.log"
    later_file = tmp_path / f"grok_search_{later:%Y%m%d}.log"
    assert today_file.read_text(encoding="utf-8") == "today\n"
    assert later_file.read_text(encoding="utf-8") == "three days later\nstill later\n"


def test_only_the_newest_files_are_kept(tmp_path):
    today = datetime.now()
    for days in range(1, 11):
        (tmp_path / f"grok_search_{today - timedelta(days=days):%Y%m%d}.log").write_text("old", encoding="utf-8")
    (tmp_path / "grok_search_notes.txt").write_text("not a dated log", encoding="utf-8")
    (tmp_path / "other.log").write_text("not ours", encoding="utf-8")
    handler = DailyFileHandler(tmp_path, retention=3)
    try:
        handler.handle(_record("x", today))
    finally:
        handler.close()
    dated = sorted(p.name for p in tmp_path.glob("grok_search_????????.log"))
    expected = sorted(f"grok_search_{today - timedelta(days=d):%Y%m%d}.log" for d in (0, 1, 2))
    assert dated == expected
    assert (tmp_path / "grok_search_notes.txt").exists() and (tmp_path / "other.log").exists()


def test_day_change_prunes_too(tmp_path):
    start = datetime.now()
    handler = DailyFileHandler(tmp_path, retention=2)
    try:
        for days in range(5):
            handler.handle(_record(f"day {days}", start + timedelta(days=days)))
    finally:
        handler.close()
    assert sorted(p.name for p in tmp_path.glob("grok_search_*.log")) == [
        f"grok_search_{start + timedelta(days=d):%Y%m%d}.log" for d in (3, 4)]


def test_fewer_old_files_than_the_limit_are_all_kept(tmp_path):
    # Regression: with retention=7 and 4 or 5 older files the slice start went negative and kept 2 or 1.
    today = datetime.now()
    for old in range(0, 8):
        directory = tmp_path / f"n{old}"
        directory.mkdir()
        for days in range(1, old + 1):
            (directory / f"grok_search_{today - timedelta(days=days):%Y%m%d}.log").write_text("old", encoding="utf-8")
        handler = DailyFileHandler(directory, retention=7)
        handler.close()
        # The handler opens today's file at construction; it counts towards the 7.
        kept = len(list(directory.glob("grok_search_????????.log")))
        assert kept == min(old, 6) + 1, (old, kept)


def test_daily_use_settles_at_the_retention_count(tmp_path):
    start = datetime.now()
    handler = DailyFileHandler(tmp_path, retention=7)
    counts = []
    try:
        for days in range(14):
            handler.handle(_record(f"day {days}", start + timedelta(days=days)))
            counts.append(len(list(tmp_path.glob("grok_search_????????.log"))))
    finally:
        handler.close()
    assert counts == [1, 2, 3, 4, 5, 6, 7] + [7] * 7
    assert sorted(p.name for p in tmp_path.glob("grok_search_*.log")) == [
        f"grok_search_{start + timedelta(days=d):%Y%m%d}.log" for d in range(7, 14)]


# ------------------------------------------------- client notifications (log_info / log_warning / log_error)
import asyncio  # noqa: E402

import anyio  # noqa: E402
import pytest  # noqa: E402
from fastmcp import Client, Context  # noqa: E402

from grok_search import fetching  # noqa: E402
from grok_search.logger import log_error, log_info, log_warning  # noqa: E402


class BrokenCtx:
    """A client whose log notifications fail, as a closed transport or a call outside a request does."""

    def __init__(self, exc=RuntimeError("client transport closed")):
        self.calls = []
        self.exc = exc

    async def _boom(self, message, *args, **kwargs):
        self.calls.append(message)
        raise self.exc

    info = warning = error = debug = _boom


class RecordingCtx:
    def __init__(self):
        self.calls = []

    async def info(self, message, *args, **kwargs):
        self.calls.append(("info", message))

    async def warning(self, message, *args, **kwargs):
        self.calls.append(("warning", message))

    async def error(self, message, *args, **kwargs):
        self.calls.append(("error", message))


@pytest.fixture
def file_records(monkeypatch):
    seen = []
    monkeypatch.setattr(logger_module.logger, "info", lambda msg, *a, **k: seen.append(("info", msg)))
    monkeypatch.setattr(logger_module.logger, "warning", lambda msg, *a, **k: seen.append(("warning", msg)))
    monkeypatch.setattr(logger_module.logger, "error", lambda msg, *a, **k: seen.append(("error", msg)))
    return seen


def test_log_info_without_debug_sends_and_writes_nothing(file_records):
    ctx = RecordingCtx()
    asyncio.run(log_info(ctx, "content: <the whole Grok answer>", False))
    assert ctx.calls == [] and file_records == []


def test_log_info_with_debug_writes_the_file_and_notifies_the_client(file_records):
    ctx = RecordingCtx()
    asyncio.run(log_info(ctx, "Begin Fetch: https://example.com/", True))
    assert ctx.calls == [("info", "Begin Fetch: https://example.com/")]
    assert file_records == [("info", "Begin Fetch: https://example.com/")]


@pytest.mark.parametrize("exc", [RuntimeError("no request context"), anyio.BrokenResourceError(), anyio.ClosedResourceError()])
def test_a_failing_client_never_breaks_the_caller(file_records, exc):
    ctx = BrokenCtx(exc)
    asyncio.run(log_info(ctx, "i", True))
    asyncio.run(log_warning(ctx, "w"))
    asyncio.run(log_error(ctx, "e"))
    assert ctx.calls == ["i", "w", "e"]
    assert file_records == [("info", "i"), ("warning", "w"), ("error", "e")]  # the file still gets every record


def test_warnings_and_errors_reach_the_client_without_debug(file_records):
    ctx = RecordingCtx()
    asyncio.run(log_warning(ctx, "w"))
    asyncio.run(log_error(ctx, "e"))
    asyncio.run(log_info(None, "no client", True))
    assert ctx.calls == [("warning", "w"), ("error", "e")]


def test_cancellation_is_not_swallowed():
    ctx = BrokenCtx(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(log_info(ctx, "i", True))


def _paid_extract(calls):
    async def extract(url, *args, **kwargs):
        calls.append(url)
        return "Paid extract text. " * 200  # 3800 characters: above GROK_FETCH_MIN_CHARS, so no Firecrawl
    return extract


def test_fetch_page_keeps_the_paid_result_when_the_client_log_fails(monkeypatch):
    monkeypatch.setenv("GROK_DEBUG", "true")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(fetching, "_call_tavily_extract", _paid_extract(calls))
    ctx = BrokenCtx()
    out = asyncio.run(fetching.fetch_page("https://example.com/page", ctx=ctx))
    assert calls == ["https://example.com/page"]
    assert out.startswith("[web_fetch] source=tavily") and "Paid extract text." in out
    assert any(message.startswith("Fetch Finished") for message in ctx.calls)


@pytest.mark.parametrize("debug", ["true", "false"])
def test_web_fetch_survives_a_closed_client_transport(monkeypatch, debug):
    from grok_search import server

    monkeypatch.setenv("GROK_DEBUG", debug)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    attempts = []

    async def broken_info(self, message, *args, **kwargs):
        attempts.append(message)
        raise anyio.BrokenResourceError()

    monkeypatch.setattr(Context, "info", broken_info)
    monkeypatch.setattr(fetching, "_call_tavily_extract", _paid_extract([]))

    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool("web_fetch", {"url": "https://example.com/page"}, raise_on_error=False)

    result = asyncio.run(go())
    text = next(block.text for block in result.content if block.type == "text")
    assert not result.is_error and "Paid extract text." in text
    assert bool(attempts) == (debug == "true")  # without GROK_DEBUG nothing is pushed to the client
