import json

import pytest
from fastmcp import Client

from grok_search import server


async def toggle(arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool("toggle_builtin_tools", arguments)
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A fake home directory; the server's cwd is a plain folder inside it (no .git, no .claude)."""
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    monkeypatch.setattr(server.Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(work)
    return home


@pytest.mark.asyncio
async def test_refuses_to_guess_and_writes_nothing(home, tmp_path):
    data = await toggle({"action": "on"})
    assert data["blocked"] is None and "project_dir" in data["error"]
    assert not list(tmp_path.rglob("settings.json"))


@pytest.mark.asyncio
async def test_explicit_project_dir(home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    on = await toggle({"action": "on", "project_dir": str(project)})
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert on["blocked"] is True and on["project_dir_source"] == "argument"
    assert settings["permissions"]["deny"] == ["WebFetch", "WebSearch"]
    off = await toggle({"action": "off", "project_dir": str(project)})
    assert off["blocked"] is False and off["deny_list"] == []


@pytest.mark.asyncio
async def test_git_root_is_found_from_a_subdirectory(home, monkeypatch):
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    monkeypatch.chdir(repo / "src")
    data = await toggle({"action": "status"})
    assert data["project_dir"] == str(repo.resolve()) and data["project_dir_source"] == "cwd"


@pytest.mark.asyncio
async def test_claude_project_dir_env(home, monkeypatch, tmp_path):
    project = tmp_path / "from-env"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    data = await toggle({"action": "status"})
    assert data["project_dir"] == str(project.resolve()) and data["project_dir_source"] == "CLAUDE_PROJECT_DIR"


@pytest.mark.asyncio
async def test_filesystem_root_is_rejected(home):
    data = await toggle({"action": "on", "project_dir": "/"})
    assert data["blocked"] is None and "根目录" in data["error"]


@pytest.mark.asyncio
async def test_corrupt_settings_are_left_alone(home, tmp_path):
    project = tmp_path / "broken"
    (project / ".claude").mkdir(parents=True)
    path = project / ".claude" / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    data = await toggle({"action": "on", "project_dir": str(project)})
    assert data["blocked"] is None and "error" in data
    assert path.read_text(encoding="utf-8") == "{not json"
