import logging
import os
import re
from datetime import datetime
from pathlib import Path
from .config import config

logger = logging.getLogger("grok_search")
logger.setLevel(getattr(logging, config.log_level, logging.INFO))

_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

_LOG_NAME_RE = re.compile(r"^grok_search_(\d{8})\.log$")


class DailyFileHandler(logging.FileHandler):
    """Appends to <dir>/grok_search_YYYYMMDD.log for the local date of each record.

    Every Claude Code session starts its own server and they all share the directory, so files are
    never renamed (unlike TimedRotatingFileHandler, whose rollover in one process would delete or
    overwrite the file another process just rotated): a process simply moves on to the new day's
    file with its first record after midnight. Only the newest `retention` dated files are kept;
    older ones are deleted at start-up and on each change of day.
    """

    def __init__(self, directory, retention: int = 7):
        self._directory = Path(directory)
        self._retention = max(1, int(retention))
        self._day = datetime.now().strftime("%Y%m%d")
        super().__init__(self._path_for(self._day), encoding="utf-8")
        self.prune()

    def _path_for(self, day: str) -> Path:
        return self._directory / f"grok_search_{day}.log"

    def emit(self, record: logging.LogRecord) -> None:
        # Handler.handle() holds the handler lock while emit() runs.
        day = datetime.fromtimestamp(record.created).strftime("%Y%m%d")
        if day != self._day:
            self._day = day
            if self.stream is not None:
                try:
                    self.stream.close()
                finally:
                    self.stream = None  # FileHandler.emit reopens baseFilename
            self.baseFilename = os.path.abspath(self._path_for(day))
            self.prune()
        super().emit(record)

    def prune(self) -> list[str]:
        """Delete dated log files beyond the newest `retention`; the current day's file is always kept."""
        try:
            days = sorted(
                (match.group(1), entry)
                for entry in os.listdir(self._directory)
                if (match := _LOG_NAME_RE.match(entry)) and match.group(1) != self._day
            )
        except OSError:
            return []
        # The current day's file counts towards the limit even before its first record is written.
        # Clamp the start: with fewer old files than retention-1 a negative index would count from the end.
        keep = {day for day, _ in days[max(0, len(days) - (self._retention - 1)):]} if self._retention > 1 else set()
        removed = []
        for day, entry in days:
            if day in keep:
                continue
            try:
                os.remove(self._directory / entry)
                removed.append(entry)
            except OSError:
                pass  # another server removed it first, or it is not ours to delete
        return removed


def _setup_file_logging() -> None:
    try:
        log_dir = config.log_dir
        if log_dir is None:
            raise OSError("no usable log directory")
        file_handler = DailyFileHandler(log_dir, config.log_retention_days)
        file_handler.setLevel(getattr(logging, config.log_level, logging.INFO))
        file_handler.setFormatter(_formatter)
        logger.addHandler(file_handler)
    except OSError:
        logger.addHandler(logging.NullHandler())


_setup_file_logging()


async def _notify(ctx, level: str, message: str) -> None:
    """Send a log notification to the MCP client; a failure there never reaches the tool.

    The client may be gone (closed transport: anyio.BrokenResourceError / ClosedResourceError) or the
    call may run outside a request; the tool's result matters more than its progress messages.
    Cancellation (a BaseException) still propagates.
    """
    if not ctx:
        return
    try:
        await getattr(ctx, level)(message)
    except Exception:
        pass


async def log_info(ctx, message: str, is_debug: bool = False):
    """Debug-gated info. GROK_DEBUG on (callers pass config.debug_enabled): written to the log file and
    sent to the client; off: dropped. Messages can be long (prompts, answers), so nothing is sent by default."""
    if not is_debug:
        return
    logger.info(message)
    await _notify(ctx, "info", message)


async def log_warning(ctx, message: str):
    """Always written to the log file and sent to the client."""
    logger.warning(message)
    await _notify(ctx, "warning", message)


async def log_error(ctx, message: str):
    """Always written to the log file and sent to the client, regardless of GROK_DEBUG."""
    logger.error(message)
    await _notify(ctx, "error", message)
