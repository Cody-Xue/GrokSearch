"""Grok endpoint settings: URL/key normalisation and the timeout settings."""
import pytest

from grok_search.config import config


def test_timeout_settings_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("GROK_READ_TIMEOUT_S", raising=False)
    monkeypatch.delenv("GROK_SEARCH_TIMEOUT_S", raising=False)
    assert (config.read_timeout_s, config.search_timeout_s) == (180.0, 300.0)
    monkeypatch.setenv("GROK_READ_TIMEOUT_S", "60")
    monkeypatch.setenv("GROK_SEARCH_TIMEOUT_S", "0")
    assert (config.read_timeout_s, config.search_timeout_s) == (60.0, 0.0)
    monkeypatch.setenv("GROK_READ_TIMEOUT_S", "nonsense")
    assert config.read_timeout_s == 180.0


def test_config_strips_url_whitespace_trailing_slash_and_key(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "  https://cpa.example.test/v1/ \n")
    monkeypatch.setenv("GROK_API_KEY", " sk-abc123456789\n")
    assert config.grok_api_url == "https://cpa.example.test/v1"
    assert config.grok_api_key == "sk-abc123456789"
    monkeypatch.setenv("GROK_API_URL", " / ")
    with pytest.raises(ValueError):
        config.grok_api_url
