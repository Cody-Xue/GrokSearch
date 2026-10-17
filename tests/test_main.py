import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run_main_with(code: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GROK_", "TAVILY_", "FIRECRAWL_"))}
    env.update(HOME=str(tmp_path / "home"), PYTHONPATH=SRC, GROK_LOG_DIR=str(tmp_path / "logs"))
    script = "import grok_search.server as s\n" + code + "\ns.main()\n"
    return subprocess.run([sys.executable, "-c", script], env=env, cwd=str(tmp_path), capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)


def test_main_reports_a_crash_with_non_zero_status(tmp_path):
    (tmp_path / "home").mkdir()
    proc = _run_main_with(
        "def boom(*a, **k):\n    raise RuntimeError('transport exploded')\ns.mcp.run = boom", tmp_path)
    assert proc.returncode == 1
    assert "RuntimeError: transport exploded" in proc.stderr and "Traceback" in proc.stderr
    logs = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").glob("grok_search_*.log"))
    assert "grok-search server crashed" in logs and "transport exploded" in logs


def test_main_clean_exit_and_keyboard_interrupt_exit_zero(tmp_path):
    (tmp_path / "home").mkdir()
    clean = _run_main_with("s.mcp.run = lambda *a, **k: None", tmp_path)
    assert clean.returncode == 0 and clean.stderr == ""
    interrupted = _run_main_with("def stop(*a, **k):\n    raise KeyboardInterrupt\ns.mcp.run = stop", tmp_path)
    assert interrupted.returncode == 0 and "Traceback" not in interrupted.stderr
    exited = _run_main_with("def stop(*a, **k):\n    raise SystemExit(3)\ns.mcp.run = stop", tmp_path)
    assert exited.returncode == 3
