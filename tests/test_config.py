import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from grok_search import config as config_module
from grok_search.config import Config, apply_online_suffix, mask_url


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """A new Config instance with its own HOME and cwd; the module-level singleton is left alone."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    monkeypatch.setattr(Config, "_instance", None)
    monkeypatch.setattr(config_module, "_ENV_WARNINGS", {})
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    for name in ("GROK_MODEL", "GROK_API_URL", "GROK_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY",
                 "TAVILY_API_URL", "FIRECRAWL_API_URL", "GROK_LOG_DIR"):
        monkeypatch.delenv(name, raising=False)
    cfg = Config()
    cfg.home, cfg.work, cfg.tmp = home, work, tmp_path / "tmp"
    return cfg


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


# ------------------------------------------------------------------ env parsing
@pytest.mark.parametrize("value, expected", [
    ("", True), ("   ", True), ("enabled", True), ("y", True), ("t", True),
    ("false", False), ("OFF", False), (" no ", False), ("0", False), ("Yes", True), ("1", True),
])
def test_env_bool_blank_or_unknown_keeps_default(fresh, monkeypatch, value, expected):
    monkeypatch.setenv("TAVILY_ENABLED", value)
    assert fresh.tavily_enabled is expected
    warned = any(w.startswith("TAVILY_ENABLED=") for w in fresh.config_warnings())
    assert warned is (value.strip().lower() in ("enabled", "y", "t"))


def test_unknown_bool_is_logged_once(fresh, monkeypatch, caplog):
    monkeypatch.setenv("GROK_VERIFY_IDS", "enabled")
    caplog.set_level("WARNING", logger="grok_search")
    for _ in range(3):
        assert fresh.verify_ids is True
    assert [r.getMessage() for r in caplog.records].count("GROK_VERIFY_IDS='enabled' 无法识别，已使用默认值 True") == 1


def test_unparseable_numbers_warn_and_keep_default(fresh, monkeypatch):
    monkeypatch.setenv("GROK_MAX_CONCURRENCY", "8.0")
    monkeypatch.setenv("GROK_MODELS_CACHE_TTL_S", "nan")
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "")
    assert fresh.max_concurrency == 16 and fresh.models_cache_ttl_s == 600.0 and fresh.retry_max_attempts == 3
    warnings = fresh.config_warnings()
    assert any(w.startswith("GROK_MAX_CONCURRENCY='8.0'") for w in warnings)
    assert any(w.startswith("GROK_MODELS_CACHE_TTL_S='nan'") for w in warnings)
    assert not any(w.startswith("GROK_RETRY_MAX_ATTEMPTS") for w in warnings)
    monkeypatch.setenv("GROK_MAX_CONCURRENCY", "8")
    assert fresh.max_concurrency == 8
    assert not any(w.startswith("GROK_MAX_CONCURRENCY") for w in fresh.config_warnings())


def test_unknown_log_level_and_style_warn(fresh, monkeypatch):
    monkeypatch.setenv("GROK_LOG_LEVEL", "bogus")
    monkeypatch.setenv("GROK_SEARCH_STYLE", "verbose")
    assert fresh.log_level == "INFO" and fresh.search_style == "concise"
    monkeypatch.setenv("GROK_LOG_LEVEL", "warn")
    assert fresh.log_level == "WARNING"
    info = fresh.get_config_info()
    assert any(w.startswith("GROK_SEARCH_STYLE='verbose'") for w in info["config_warnings"])


def test_get_config_info_lists_every_documented_variable(fresh):
    info = fresh.get_config_info()
    for name in ("GROK_RETRY_MULTIPLIER", "GROK_RETRY_MAX_WAIT", "GROK_VERIFY_TIMEOUT_S", "GROK_VERIFY_MAILTO",
                 "GROK_CONFIG_FILE", "GROK_LOG_RETENTION_DAYS"):
        assert name in info, name
    assert "config_warnings" not in info


# --------------------------------------------------------------------- masking
@pytest.mark.parametrize("length", [1, 7, 8, 9, 10, 13, 16, 20, 23, 24, 28, 31, 32, 40, 64])
def test_mask_reveals_at_most_a_quarter_and_four_chars_below_24(length):
    key = "".join(chr(ord("a") + i % 26) for i in range(length))
    masked = Config._mask_api_key(key)
    visible = len(masked.replace("***", ""))
    assert visible <= length // 4
    assert visible <= (4 if length < 24 else 8)
    if length >= 32:
        assert masked == f"{key[:4]}***{key[-4:]}"


def test_mask_examples():
    assert Config._mask_api_key("123456789") == "***89"
    assert Config._mask_api_key("sk-1234567") == "***67"
    assert Config._mask_api_key("0123456789abcdef") == "***cdef"
    assert Config._mask_api_key("1234567") == "***"
    assert Config._mask_api_key("") == "***"


def test_mask_url_hides_credentials():
    assert mask_url("http://user:pw@127.0.0.1:8080/v1") == "http://***@127.0.0.1:8080/v1"
    assert mask_url("https://tavily.example/?api_key=tvly-secret&x") == "https://tavily.example/?api_key=***&***"
    assert mask_url("https://a.example/v1#token") == "https://a.example/v1#***"
    assert mask_url("https://api.x.ai/v1") == "https://api.x.ai/v1"


def test_get_config_info_masks_urls_and_short_keys(fresh, monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "http://user:url-secret-pw@127.0.0.1:9/v1")
    monkeypatch.setenv("GROK_API_KEY", "sk-1234567")
    monkeypatch.setenv("TAVILY_API_URL", "http://127.0.0.1:9/?api_key=tvly-in-url-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-12345")
    info = json.dumps(fresh.get_config_info(), ensure_ascii=False)
    for secret in ("url-secret-pw", "tvly-in-url-secret", "1234567", "12345"):
        assert secret not in info, secret
    assert fresh.get_config_info()["GROK_API_URL"] == "http://***@127.0.0.1:9/v1"


# ------------------------------------------------------------- config file
@pytest.mark.parametrize("content", ["[]", "null", '"grok-4.7"', "42", "{not json", b"\xff\xfe\x00bad"])
def test_malformed_config_file_is_ignored_and_reported(fresh, content):
    path = fresh.home / ".config" / "grok-search" / "config.json"
    path.parent.mkdir(parents=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    assert fresh.grok_model == Config._DEFAULT_MODEL and fresh.grok_model_source == "default"
    info = fresh.get_config_info()
    assert any(str(path) in w for w in info["config_warnings"])
    assert fresh.set_model("grok-4.7") is None
    assert json.loads(path.read_text(encoding="utf-8")) == {"model": "grok-4.7"}


def test_reading_config_creates_no_directories(fresh):
    assert fresh.grok_model == Config._DEFAULT_MODEL
    fresh.get_config_info()
    assert not (fresh.home / ".config" / "grok-search" / "config.json").exists()
    assert _tree(fresh.work) == []


def test_unwritable_config_dir_uses_private_temp_dir_never_cwd(fresh):
    (fresh.home / ".config").write_text("not a directory", encoding="utf-8")
    assert fresh.grok_model == Config._DEFAULT_MODEL  # reading does not crash
    assert fresh.set_model("grok-4.7") is None
    assert fresh.grok_model == "grok-4.7" and fresh.grok_model_source == "switch_model"
    path = fresh.config_file
    assert path.parent.parent == fresh.tmp and path.parent.name.startswith("grok-search-")
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8")) == {"model": "grok-4.7"}
    assert fresh.config_file_is_temporary
    info = fresh.get_config_info()
    assert any("临时目录" in w for w in info["config_warnings"])
    assert "重启后不会保留" in info["GROK_MODEL_NOTE"]
    assert _tree(fresh.work) == []  # nothing in the project


def test_project_config_in_cwd_is_never_read(fresh):
    (fresh.home / ".config").write_text("not a directory", encoding="utf-8")
    (fresh.work / ".grok-search").mkdir()
    (fresh.work / ".grok-search" / "config.json").write_text('{"model": "repo-chosen-model"}', encoding="utf-8")
    assert fresh.grok_model == Config._DEFAULT_MODEL


def test_save_failure_still_switches_in_process(fresh, monkeypatch):
    def deny(path, data):
        raise PermissionError("read-only")

    monkeypatch.setattr(Config, "_write_json", staticmethod(deny))
    warning = fresh.set_model("grok-4.7")
    assert "无法保存配置文件" in warning
    assert fresh.grok_model == "grok-4.7" and "未能保存" in fresh.grok_model_note


# ----------------------------------------------------------------- model
def test_model_source_tracks_switch_model(fresh, monkeypatch):
    monkeypatch.setenv("GROK_MODEL", "env-model")
    assert fresh.grok_model == "env-model" and fresh.grok_model_source == "env"
    assert fresh.grok_model_note is None
    fresh.set_model("  grok-4.7  ")
    assert fresh.grok_model == "grok-4.7" and fresh.grok_model_source == "switch_model"
    assert "GROK_MODEL=env-model" in fresh.grok_model_note
    # A restart: env wins again over the saved value.
    monkeypatch.setattr(Config, "_instance", None)
    again = Config()
    assert again.grok_model == "env-model" and again.grok_model_source == "env"
    monkeypatch.delenv("GROK_MODEL")
    monkeypatch.setattr(Config, "_instance", None)
    third = Config()
    assert third.grok_model == "grok-4.7" and third.grok_model_source == "config"


def test_switch_without_env_is_persistent(fresh):
    fresh.set_model("grok-4.7")
    assert "仍然生效" in fresh.grok_model_note


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_set_model_rejects_blank(fresh, blank):
    with pytest.raises(ValueError):
        fresh.set_model(blank)
    assert fresh.grok_model == Config._DEFAULT_MODEL


def test_blank_env_model_counts_as_unset(fresh, monkeypatch):
    monkeypatch.setenv("GROK_MODEL", "   ")
    assert fresh.grok_model == Config._DEFAULT_MODEL and fresh.grok_model_source == "default"


def test_online_suffix_rule():
    assert apply_online_suffix("x-ai/grok-4", "https://openrouter.ai/api/v1") == "x-ai/grok-4:online"
    assert apply_online_suffix("x-ai/grok-4:online", "https://OpenRouter.ai/api/v1") == "x-ai/grok-4:online"
    assert apply_online_suffix("grok-4", "https://cpa.example/v1") == "grok-4"


# -------------------------------------------------------------- log directory
def test_relative_log_dir_lives_under_home(fresh, monkeypatch):
    monkeypatch.setenv("GROK_LOG_DIR", "logs")
    assert fresh.log_dir == fresh.home / ".config" / "grok-search" / "logs"


def test_log_dir_falls_back_to_private_temp_dir_not_cwd(fresh, monkeypatch):
    monkeypatch.setenv("GROK_LOG_DIR", "logs")
    (fresh.home / ".config").write_text("not a directory", encoding="utf-8")
    log_dir = fresh.log_dir
    assert log_dir is not None and log_dir.parent.parent == fresh.tmp and log_dir.name == "logs"
    assert _tree(fresh.work) == []
    assert any("日志改写到临时目录" in w for w in fresh.get_config_info()["config_warnings"])


def test_unwritable_absolute_log_dir_falls_back(fresh, monkeypatch, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running as a user that ignores permissions")
        monkeypatch.setenv("GROK_LOG_DIR", str(locked))
        log_dir = fresh.log_dir
        assert log_dir is not None and log_dir.parent.parent == fresh.tmp
    finally:
        locked.chmod(0o700)
