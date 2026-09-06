import os
import tempfile
from pathlib import Path

# The logger is initialized at import time. Keep test logs out of user config.
os.environ.setdefault("GROK_LOG_DIR", str(Path(tempfile.gettempdir()) / "grok-search-tests"))
