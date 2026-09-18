import os
import tempfile
from pathlib import Path

import pytest

# The logger is initialized at import time. Keep test logs out of user config.
os.environ.setdefault("GROK_LOG_DIR", str(Path(tempfile.gettempdir()) / "grok-search-tests"))
# Network-facing post-verification and the optional planning tools are enabled explicitly by tests.
os.environ["GROK_VERIFY_IDS"] = "false"
os.environ["GROK_VERIFY_URLS"] = "false"
os.environ["GROK_PLANNING_TOOLS"] = "false"

from grok_search.throttle import breaker  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_breaker():
    breaker.reset()
    yield
    breaker.reset()
