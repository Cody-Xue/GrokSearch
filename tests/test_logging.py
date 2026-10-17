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
