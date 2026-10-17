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
async def test_an_unmarked_working_directory_is_the_project(home, tmp_path):
    # v1.13.1 field report: without CLAUDE_PROJECT_DIR and a .git/.claude above it, the tool used to refuse.
    work = home / "work"
    status = await toggle({"action": "status"})
    assert status["project_dir"] == str(work.resolve()) and status["project_dir_source"] == "working_directory"
    assert status["warning"].startswith("没有 CLAUDE_PROJECT_DIR") and not list(tmp_path.rglob("settings.json"))
    on = await toggle({"action": "on"})
    assert on["blocked"] is True and on["file"] == str((work / ".claude" / "settings.json").resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["argument", "CLAUDE_PROJECT_DIR", "working_directory"])
@pytest.mark.parametrize("action", ["on", "off", "status"])
async def test_the_home_directory_is_never_the_project(home, monkeypatch, tmp_path, source, action):
    # ~/.claude/settings.json is the user-level file: "on" there blocked the tools in every project.
    arguments = {"action": action}
    if source == "argument":
        arguments["project_dir"] = str(home)
    elif source == "CLAUDE_PROJECT_DIR":
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(home))
    else:
        monkeypatch.chdir(home)
    data = await toggle(arguments)
    assert data["blocked"] is None and "用户主目录" in data["error"] and "未做任何修改" in data["error"]
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


async def toggle_raw(arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool("toggle_builtin_tools", arguments)
    assert not result.is_error
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@pytest.fixture
def project(home, tmp_path):
    path = tmp_path / "proj"
    (path / ".claude").mkdir(parents=True)
    return path


def _deny(path):
    return json.loads(path.read_text(encoding="utf-8"))["permissions"]["deny"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["disabel", "true", "enable", "disable", "Disable "])
async def test_unknown_or_ambiguous_actions_are_rejected(project, action):
    data = await toggle_raw({"action": action, "project_dir": str(project)})
    assert data["blocked"] is None and "on" in data["error"] and "off" in data["error"] and "status" in data["error"]
    if action.strip().lower() in ("enable", "disable"):
        assert "歧义" in data["error"]
    assert not (project / ".claude" / "settings.json").exists()


@pytest.mark.asyncio
async def test_block_and_allow_aliases_match_on_and_off(project):
    settings = project / ".claude" / "settings.json"
    blocked = await toggle_raw({"action": " Block ", "project_dir": str(project)})
    assert blocked["blocked"] is True and _deny(settings) == ["WebFetch", "WebSearch"]
    allowed = await toggle_raw({"action": "allow", "project_dir": str(project)})
    assert allowed["blocked"] is False and _deny(settings) == []
    assert allowed["effective"] == {"WebFetch": "allowed", "WebSearch": "allowed"}


@pytest.mark.asyncio
async def test_action_schema_lists_the_valid_values():
    async with Client(server.mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == "toggle_builtin_tools")
    assert tool.input_schema["properties"]["action"]["enum"] == ["on", "off", "status", "block", "allow"]


@pytest.mark.asyncio
async def test_settings_local_json_denies_are_reported(project):
    local = project / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"permissions": {"deny": ["WebFetch", "WebSearch"]}}), encoding="utf-8")
    status = await toggle_raw({"action": "status", "project_dir": str(project)})
    assert status["blocked"] is False  # settings.json itself does not block them
    assert status["effective"] == {"WebFetch": "blocked", "WebSearch": "blocked"}
    assert status["denied_elsewhere"] == {str(local.resolve()): ["WebFetch", "WebSearch"]}
    assert "已禁用" in status["message"] and "settings.local.json" in status["message"]
    off = await toggle_raw({"action": "off", "project_dir": str(project)})
    assert off["blocked"] is False and "仍然屏蔽" in off["message"] and "settings.local.json" in off["warning"]
    assert off["effective"]["WebSearch"] == "blocked"
    assert _deny(local) == ["WebFetch", "WebSearch"]  # never modified


@pytest.mark.asyncio
async def test_user_level_denies_are_reported(project, home):
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"deny": ["WebSearch"]}}), encoding="utf-8")
    status = await toggle_raw({"action": "status", "project_dir": str(project)})
    assert status["effective"] == {"WebFetch": "allowed", "WebSearch": "blocked"}
    assert "WebSearch" in status["message"]


@pytest.mark.asyncio
async def test_settings_symlink_outside_the_project_is_refused(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.json"
    victim.write_text(json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}}), encoding="utf-8")
    (project / ".claude" / "settings.json").symlink_to(victim)
    data = await toggle_raw({"action": "on", "project_dir": str(project)})
    assert data["blocked"] is None and "符号链接" in data["error"]
    assert _deny(victim) == ["Bash(rm:*)"]


@pytest.mark.asyncio
async def test_dangling_symlink_outside_the_project_is_not_created(project, tmp_path):
    target = tmp_path / "outside" / "created.json"
    (project / ".claude" / "settings.json").symlink_to(target)
    data = await toggle_raw({"action": "on", "project_dir": str(project)})
    assert data["blocked"] is None and not target.exists()


@pytest.mark.asyncio
async def test_symlinked_claude_dir_outside_the_project_is_refused(home, tmp_path):
    project = tmp_path / "linked"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (project / ".claude").symlink_to(elsewhere, target_is_directory=True)
    data = await toggle_raw({"action": "on", "project_dir": str(project)})
    assert data["blocked"] is None and list(elsewhere.iterdir()) == []


@pytest.mark.asyncio
async def test_symlink_inside_the_project_reports_the_real_file(project):
    real = project / "config" / "claude-settings.json"
    real.parent.mkdir()
    real.write_text("{}", encoding="utf-8")
    link = project / ".claude" / "settings.json"
    link.symlink_to(real)
    data = await toggle_raw({"action": "on", "project_dir": str(project)})
    assert data["blocked"] is True and data["file"] == str(real.resolve()) and data["symlink"] == str(link)
    assert link.is_symlink() and _deny(real) == ["WebFetch", "WebSearch"]
    assert [p.name for p in (project / ".claude").iterdir()] == ["settings.json"]  # no temp files left


@pytest.mark.asyncio
async def test_read_only_settings_file_is_not_replaced(project):
    import os
    settings = project / ".claude" / "settings.json"
    settings.write_text('{"permissions": {"deny": []}}', encoding="utf-8")
    settings.chmod(0o444)
    try:
        if os.access(settings, os.W_OK):
            pytest.skip("running as a user that ignores permissions")
        data = await toggle_raw({"action": "on", "project_dir": str(project)})
        assert data["blocked"] is None and "写入" in data["error"]
        assert _deny(settings) == []
    finally:
        settings.chmod(0o644)


@pytest.mark.asyncio
async def test_unusable_cwd_asks_for_project_dir(home, monkeypatch):
    def gone():
        raise FileNotFoundError("cwd was removed")

    monkeypatch.setattr(server.Path, "cwd", staticmethod(gone))
    data = await toggle_raw({"action": "status"})
    assert data["blocked"] is None and "project_dir" in data["error"]
