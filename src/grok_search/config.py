import os
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

_log = logging.getLogger("grok_search")

_TRUE_VALUES = ("true", "1", "yes", "on")
_FALSE_VALUES = ("false", "0", "no", "off")
# Variables whose value could not be parsed, mapped to the warning shown by get_config_info.
_ENV_WARNINGS: dict[str, str] = {}


def _env_value(name: str) -> Optional[str]:
    """Stripped value of an environment variable; None when it is unset or blank."""
    value = os.getenv(name)
    if value is None or not value.strip():
        _ENV_WARNINGS.pop(name, None)
        return None
    return value.strip()


def _env_fallback(name: str, value: str, default):
    """Record (and log once) that a value was not understood, and return the default instead."""
    message = f"{name}={value!r} 无法识别，已使用默认值 {default!r}"
    if _ENV_WARNINGS.get(name) != message:
        _ENV_WARNINGS[name] = message
        _log.warning(message)
    return default


def _env_int(name: str, default: int) -> int:
    value = _env_value(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return _env_fallback(name, value, default)
    _ENV_WARNINGS.pop(name, None)
    return parsed


def _env_float(name: str, default: float) -> float:
    value = _env_value(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return _env_fallback(name, value, default)
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return _env_fallback(name, value, default)
    _ENV_WARNINGS.pop(name, None)
    return parsed


def _env_bool(name: str, default: bool) -> bool:
    """true/1/yes/on and false/0/no/off (any case); unset, blank or anything else keeps the default."""
    value = _env_value(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE_VALUES or lowered in _FALSE_VALUES:
        _ENV_WARNINGS.pop(name, None)
        return lowered in _TRUE_VALUES
    return _env_fallback(name, value, default)


def uses_online_suffix(api_url: str) -> bool:
    """OpenRouter needs the ':online' variant of a model for live web search."""
    return "openrouter" in (api_url or "").lower()


def apply_online_suffix(model: str, api_url: str) -> str:
    """The name to send: ':online' appended on OpenRouter, unchanged for other gateways."""
    if model and uses_online_suffix(api_url) and not model.endswith(":online"):
        return f"{model}:online"
    return model


def mask_url(url: str) -> str:
    """A URL for display: credentials in userinfo, query values and the fragment are replaced by ***."""
    if not url:
        return url
    try:
        parts = urlsplit(url.strip())
        netloc = parts.netloc
        if "@" in netloc:
            netloc = "***@" + netloc.rsplit("@", 1)[1]
        query = "&".join(
            (item.split("=", 1)[0] + "=***") if "=" in item else "***"
            for item in parts.query.split("&") if item
        )
        return urlunsplit((parts.scheme, netloc, parts.path, query, "***" if parts.fragment else ""))
    except ValueError:
        return "***"


class Config:
    _instance = None
    _SETUP_COMMAND = (
        'claude mcp add-json grok-search --scope user '
        '\'{"type":"stdio","command":"uvx","args":["--from",'
        '"git+https://github.com/Cody-Xue/GrokSearch@main","grok-search"],'
        '"env":{"GROK_API_URL":"your-api-url","GROK_API_KEY":"your-api-key"}}\''
    )
    _DEFAULT_MODEL = "grok-4-fast"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config_file = None
            cls._instance._cached_model = None
            cls._instance._model_source = None  # where _cached_model came from: env | config | default | switch_model
            cls._instance._model_persisted = False
            cls._instance._private_dir = None
            cls._instance._log_dir = None  # (GROK_LOG_DIR value, resolved directory or None)
            cls._instance._notes = {}  # topic -> problem with the config file or log directory
        return cls._instance

    # ------------------------------------------------------------ local files
    @staticmethod
    def _home_config_dir() -> Optional[Path]:
        try:
            return Path.home() / ".config" / "grok-search"
        except (RuntimeError, KeyError, OSError):  # no HOME and no passwd entry
            return None

    def _fallback_dir(self) -> Optional[Path]:
        """A private (0700) temp directory, created once per process, used when ~/.config/grok-search
        cannot be written. The working directory is never used: under Claude Code it is the project."""
        if self._private_dir is None:
            try:
                self._private_dir = Path(tempfile.mkdtemp(prefix="grok-search-"))
            except OSError:
                return None
        return self._private_dir

    def _note(self, topic: str, message: Optional[str]) -> None:
        """Remember (and log once) a local-file problem for get_config_info; None clears it."""
        if message is None:
            self._notes.pop(topic, None)
        elif self._notes.get(topic) != message:
            self._notes[topic] = message
            _log.warning(message)

    @property
    def config_file(self) -> Optional[Path]:
        """~/.config/grok-search/config.json, or the private temp directory once that could not be
        written. Reading never creates directories; None when no location can be determined."""
        if self._config_file is None:
            home_dir = self._home_config_dir()
            if home_dir is not None:
                self._config_file = home_dir / "config.json"
            else:
                fallback = self._fallback_dir()
                if fallback is None:
                    return None
                self._config_file = fallback / "config.json"
                self._note("config_file_fallback", f"无法确定用户主目录，配置保存在临时目录 {self._config_file}，服务器重启后不会保留")
        return self._config_file

    def _load_config_file(self) -> dict:
        """The config file as a dict. A missing, unreadable or non-object file counts as empty."""
        path = self.config_file
        if path is None:
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            self._note("config_file", None)
            return {}
        except (OSError, ValueError) as e:
            self._note("config_file", f"无法读取配置文件 {path}（{type(e).__name__}: {e}），已忽略")
            return {}
        if not isinstance(data, dict):
            self._note("config_file", f"配置文件 {path} 的顶层不是 JSON 对象（{type(data).__name__}），已忽略")
            return {}
        self._note("config_file", None)
        return data

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _save_config_file(self, config_data: dict) -> None:
        """Write config.json; when its directory is not writable, use the private temp directory instead."""
        path = self.config_file
        error: Optional[Exception] = None
        if path is not None:
            try:
                self._write_json(path, config_data)
                return
            except OSError as e:
                error = e
        fallback = self._fallback_dir()
        if fallback is None or (path is not None and path.parent == fallback):
            raise ValueError(f"无法保存配置文件: {error or '无法确定配置目录'}")
        target = fallback / "config.json"
        try:
            self._write_json(target, config_data)
        except OSError as e:
            raise ValueError(f"无法保存配置文件: {e}")
        self._config_file = target
        self._note(
            "config_file_fallback",
            f"{path or '~/.config/grok-search/config.json'} 不可写（{error or '无法确定用户主目录'}），"
            f"配置改存到临时目录 {target}，服务器重启后不会保留",
        )

    @property
    def config_file_is_temporary(self) -> bool:
        return self._private_dir is not None and self._config_file is not None and self._config_file.parent == self._private_dir

    # ------------------------------------------------------------------ basics
    @property
    def debug_enabled(self) -> bool:
        return _env_bool("GROK_DEBUG", False)

    @property
    def retry_max_attempts(self) -> int:
        return max(0, _env_int("GROK_RETRY_MAX_ATTEMPTS", 3))

    @property
    def retry_multiplier(self) -> float:
        return _env_float("GROK_RETRY_MULTIPLIER", 1.0)

    @property
    def retry_max_wait(self) -> int:
        return _env_int("GROK_RETRY_MAX_WAIT", 10)

    @property
    def retry_budget_s(self) -> float:
        """Total seconds one call may spend waiting between retries. 0 disables the budget."""
        return max(0.0, _env_float("GROK_RETRY_BUDGET_S", 45.0))

    @property
    def read_timeout_s(self) -> float:
        """Longest silence allowed while waiting for Grok's answer (per read, not per call).

        Buffering gateways send nothing until the answer is complete, so this also caps how
        long one attempt may think before it fails with error_type "timeout".
        """
        return max(5.0, _env_float("GROK_READ_TIMEOUT_S", 180.0))

    @property
    def search_timeout_s(self) -> float:
        """Wall-clock bound of one web_search Grok call: queueing, every attempt and every retry wait. 0 disables it."""
        return max(0.0, _env_float("GROK_SEARCH_TIMEOUT_S", 300.0))

    # -------------------------------------------------------- throttling / breaker
    @property
    def max_concurrency(self) -> int:
        return max(1, _env_int("GROK_MAX_CONCURRENCY", 16))

    @property
    def breaker_threshold(self) -> int:
        return max(1, _env_int("GROK_BREAKER_THRESHOLD", 3))

    @property
    def breaker_window_s(self) -> float:
        return max(1.0, _env_float("GROK_BREAKER_WINDOW_S", 60.0))

    @property
    def breaker_cooldown_s(self) -> float:
        return max(1.0, _env_float("GROK_BREAKER_COOLDOWN_S", 60.0))

    @property
    def breaker_max_cooldown_s(self) -> float:
        return max(self.breaker_cooldown_s, _env_float("GROK_BREAKER_MAX_COOLDOWN_S", 300.0))

    # ---------------------------------------------------------------- search
    @property
    def search_style(self) -> str:
        """concise (default) or explanatory; unknown values fall back to concise."""
        value = _env_value("GROK_SEARCH_STYLE")
        if value is None:
            return "concise"
        if value.lower() in ("explanatory", "concise"):
            return value.lower()
        return _env_fallback("GROK_SEARCH_STYLE", value, "concise")

    @property
    def models_cache_ttl_s(self) -> float:
        """How long a successful /models list is trusted; 0 refetches on every check."""
        return max(0.0, _env_float("GROK_MODELS_CACHE_TTL_S", 600.0))

    @property
    def verify_ids(self) -> bool:
        return _env_bool("GROK_VERIFY_IDS", True)

    @property
    def verify_urls(self) -> bool:
        return _env_bool("GROK_VERIFY_URLS", True)

    @property
    def verify_timeout_s(self) -> float:
        return max(1.0, _env_float("GROK_VERIFY_TIMEOUT_S", 30.0))

    @property
    def verify_mailto(self) -> str:
        return os.getenv("GROK_VERIFY_MAILTO", "").strip()

    @property
    def verify_fake_ip(self) -> str:
        """doh (default), skip or allow: URL checks for hosts the local resolver maps to fake-ip (198.18.0.0/15)."""
        value = _env_value("GROK_VERIFY_FAKE_IP")
        if value is None:
            return "doh"
        if value.lower() in ("doh", "skip", "allow"):
            return value.lower()
        return _env_fallback("GROK_VERIFY_FAKE_IP", value, "doh")

    @property
    def verify_doh_url(self) -> str:
        """DNS-over-HTTPS JSON endpoint used by GROK_VERIFY_FAKE_IP=doh (sent through the configured proxy)."""
        return os.getenv("GROK_VERIFY_DOH_URL", "").strip() or "https://cloudflare-dns.com/dns-query"

    @property
    def planning_tools_enabled(self) -> bool:
        return _env_bool("GROK_PLANNING_TOOLS", False)

    # ----------------------------------------------------------------- fetch
    @property
    def fetch_min_chars(self) -> int:
        return max(0, _env_int("GROK_FETCH_MIN_CHARS", 2000))

    @property
    def fetch_max_chars(self) -> int:
        return max(1000, _env_int("GROK_FETCH_MAX_CHARS", 40000))

    @property
    def fetch_max_chars_cjk(self) -> int:
        """Default page size when CJK text dominates: CJK costs about one token per character, so a
        40,000-character page can exceed the ~25k-token MCP result limit of Claude Code."""
        return max(1000, _env_int("GROK_FETCH_MAX_CHARS_CJK", 20000))

    @property
    def fetch_arxiv_budget_s(self) -> float:
        """Wall-clock budget of the arXiv API fast path in web_fetch (spacing gate, retry and request included)."""
        return max(1.0, _env_float("GROK_FETCH_ARXIV_BUDGET_S", 8.0))

    @property
    def tavily_extract_timeout_s(self) -> float:
        return max(5.0, _env_float("TAVILY_EXTRACT_TIMEOUT_S", 30.0))

    @property
    def fetch_snapshot_ttl_s(self) -> float:
        """Idle lifetime of a web_fetch page snapshot used for paging."""
        return max(30.0, _env_float("GROK_FETCH_SNAPSHOT_TTL_S", 900.0))

    # ------------------------------------------------------------- endpoints
    @property
    def grok_api_url(self) -> str:
        # Normalised once so /chat/completions and /models are joined the same way:
        # "https://host/v1/" must not turn into "https://host/v1//chat/completions".
        url = (os.getenv("GROK_API_URL") or "").strip().rstrip("/")
        if not url:
            raise ValueError(
                f"Grok API URL 未配置！\n"
                f"请使用以下命令配置 MCP 服务器：\n{self._SETUP_COMMAND}"
            )
        return url

    @property
    def grok_api_key(self) -> str:
        key = (os.getenv("GROK_API_KEY") or "").strip()
        if not key:
            raise ValueError(
                f"Grok API Key 未配置！\n"
                f"请使用以下命令配置 MCP 服务器：\n{self._SETUP_COMMAND}"
            )
        return key

    @property
    def tavily_enabled(self) -> bool:
        return _env_bool("TAVILY_ENABLED", True)

    @property
    def tavily_api_url(self) -> str:
        return os.getenv("TAVILY_API_URL", "https://api.tavily.com")

    @property
    def tavily_api_key(self) -> str | None:
        return os.getenv("TAVILY_API_KEY")

    @property
    def tavily_active(self) -> bool:
        """Tavily is used only when it is enabled *and* has a key."""
        return self.tavily_enabled and bool(self.tavily_api_key)

    @property
    def firecrawl_api_url(self) -> str:
        return os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v2")

    @property
    def firecrawl_api_key(self) -> str | None:
        return os.getenv("FIRECRAWL_API_KEY")

    @property
    def firecrawl_active(self) -> bool:
        return bool(self.firecrawl_api_key)

    def provider_status(self) -> dict:
        """Effective state of the optional engines, as used by web_fetch, web_map and extra_sources."""
        if not self.tavily_enabled:
            tavily = "disabled (TAVILY_ENABLED=false)"
        elif not self.tavily_api_key:
            tavily = "not configured (TAVILY_API_KEY)"
        else:
            tavily = "active"
        firecrawl = "active" if self.firecrawl_active else "not configured (FIRECRAWL_API_KEY)"
        return {"tavily": tavily, "firecrawl": firecrawl}

    @property
    def log_level(self) -> str:
        value = _env_value("GROK_LOG_LEVEL")
        if value is None:
            return "INFO"
        level = value.upper()
        if level == "WARN":
            level = "WARNING"
        if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return level
        return _env_fallback("GROK_LOG_LEVEL", value, "INFO")

    @property
    def log_retention_days(self) -> int:
        """How many daily log files are kept; older ones are deleted."""
        return max(1, _env_int("GROK_LOG_RETENTION_DAYS", 7))

    @property
    def log_dir(self) -> Optional[Path]:
        """Directory of the daily log files, created on first use.

        An absolute GROK_LOG_DIR is used as given; a relative one lives under ~/.config/grok-search.
        When it cannot be created or written the logs go to the private temp directory (never the
        working directory); None means file logging is unavailable.
        """
        raw = _env_value("GROK_LOG_DIR") or "logs"
        if self._log_dir is not None and self._log_dir[0] == raw:
            return self._log_dir[1]
        try:
            wanted: Optional[Path] = Path(raw).expanduser()
        except RuntimeError:
            wanted = Path(raw)
        if not wanted.is_absolute():
            home_dir = self._home_config_dir()
            wanted = home_dir / wanted if home_dir is not None else None
        resolved: Optional[Path] = None
        error = "无法确定用户主目录"
        if wanted is not None:
            try:
                wanted.mkdir(parents=True, exist_ok=True)
                if not os.access(wanted, os.W_OK | os.X_OK):
                    raise PermissionError(f"没有写权限: {wanted}")
                resolved = wanted
            except OSError as e:
                error = str(e) or type(e).__name__
        if resolved is None:
            fallback = self._fallback_dir()
            if fallback is not None:
                try:
                    (fallback / "logs").mkdir(exist_ok=True)
                    resolved = fallback / "logs"
                except OSError:
                    pass
            self._note("log_dir", f"日志目录 {wanted or raw} 不可用（{error}），" + (
                f"日志改写到临时目录 {resolved}" if resolved else "已停用文件日志"))
        else:
            self._note("log_dir", None)
        self._log_dir = (raw, resolved)
        return resolved

    # ------------------------------------------------------------------ model
    def _apply_model_suffix(self, model: str) -> str:
        try:
            url = self.grok_api_url
        except ValueError:
            return model
        return apply_online_suffix(model, url)

    def _resolve_model(self) -> tuple[str, str]:
        """(model, source) from the environment, then the config file, then the built-in default."""
        env_model = _env_value("GROK_MODEL")
        if env_model:
            return env_model, "env"
        file_model = self._load_config_file().get("model")
        if isinstance(file_model, str) and file_model.strip():
            return file_model.strip(), "config"
        return self._DEFAULT_MODEL, "default"

    @property
    def grok_model_source(self) -> str:
        """Where the model in use comes from: env, switch_model (this process), config (file) or default."""
        self.grok_model
        return self._model_source or "default"

    @property
    def grok_model_note(self) -> Optional[str]:
        """How long a model chosen with switch_model lasts; None for any other source."""
        if self.grok_model_source != "switch_model":
            return None
        env_model = _env_value("GROK_MODEL")
        if env_model:
            return (f"由 switch_model 设置，只在当前进程有效：环境变量 GROK_MODEL={env_model} 优先于配置文件，"
                    f"服务器重启后会恢复为 {env_model}")
        if not self._model_persisted:
            return "由 switch_model 设置，但未能保存到配置文件，只在当前进程有效"
        if self.config_file_is_temporary:
            return f"由 switch_model 设置并保存到临时目录 {self.config_file}，服务器重启后不会保留"
        return f"由 switch_model 设置并保存到 {self.config_file}，服务器重启后仍然生效"

    @property
    def grok_model(self) -> str:
        if self._cached_model is not None:
            return self._cached_model
        model, source = self._resolve_model()
        self._cached_model = self._apply_model_suffix(model)
        self._model_source = source
        return self._cached_model

    def set_model(self, model: str) -> Optional[str]:
        """Make model the default for the rest of this process and save it to the config file.

        The in-process switch always applies; returns a warning when the file could not be written.
        """
        model = (model or "").strip()
        if not model:
            raise ValueError("模型名不能为空")
        config_data = self._load_config_file()
        config_data["model"] = model
        warning = None
        try:
            self._save_config_file(config_data)
            self._model_persisted = True
        except ValueError as e:
            self._model_persisted = False
            warning = f"{e}；本次切换只在当前进程有效"
        self._cached_model = self._apply_model_suffix(model)
        self._model_source = "switch_model"
        return warning

    @staticmethod
    def _mask_api_key(key: str) -> str:
        """脱敏显示 API Key：最多显示四分之一；短于 24 个字符时最多显示末尾 4 个字符，32 个字符起首尾各 4 个。"""
        key = key or ""
        visible = min(8, len(key) // 4)
        if len(key) < 24:
            visible = min(visible, 4)
        if visible < 2:
            return "***"
        head = visible // 2 if len(key) >= 24 else 0
        tail = visible - head
        return f"{key[:head]}***{key[-tail:]}"

    def config_warnings(self) -> list[str]:
        """Unparseable environment values and config-file / log-directory problems seen so far."""
        return list(_ENV_WARNINGS.values()) + list(self._notes.values())

    def get_config_info(self) -> dict:
        """获取配置信息（API Key 和 URL 中的凭据已脱敏）"""
        try:
            api_url = mask_url(self.grok_api_url)
            api_key_raw = self.grok_api_key
            api_key_masked = self._mask_api_key(api_key_raw)
            config_status = "✅ 配置完整"
        except ValueError as e:
            api_url = "未配置"
            api_key_masked = "未配置"
            config_status = f"❌ 配置错误: {str(e)}"

        log_dir = self.log_dir
        info = {
            "GROK_API_URL": api_url,
            "GROK_API_KEY": api_key_masked,
            "GROK_MODEL": self.grok_model,
            "GROK_MODEL_SOURCE": self.grok_model_source,
        }
        if self.grok_model_note:
            info["GROK_MODEL_NOTE"] = self.grok_model_note
        config_file = self.config_file
        info.update({
            "GROK_CONFIG_FILE": str(config_file) if config_file else "不可用",
            "GROK_DEBUG": self.debug_enabled,
            "GROK_LOG_LEVEL": self.log_level,
            "GROK_LOG_DIR": str(log_dir) if log_dir else "不可用（已停用文件日志）",
            "GROK_LOG_RETENTION_DAYS": self.log_retention_days,
            "GROK_SEARCH_STYLE": self.search_style,
            "GROK_MODELS_CACHE_TTL_S": self.models_cache_ttl_s,
            "GROK_MAX_CONCURRENCY": self.max_concurrency,
            "GROK_RETRY_MAX_ATTEMPTS": self.retry_max_attempts,
            "GROK_RETRY_MULTIPLIER": self.retry_multiplier,
            "GROK_RETRY_MAX_WAIT": self.retry_max_wait,
            "GROK_RETRY_BUDGET_S": self.retry_budget_s,
            "GROK_READ_TIMEOUT_S": self.read_timeout_s,
            "GROK_SEARCH_TIMEOUT_S": self.search_timeout_s,
            "GROK_BREAKER": {
                "threshold": self.breaker_threshold,
                "window_s": self.breaker_window_s,
                "cooldown_s": self.breaker_cooldown_s,
                "max_cooldown_s": self.breaker_max_cooldown_s,
            },
            "GROK_VERIFY_IDS": self.verify_ids,
            "GROK_VERIFY_URLS": self.verify_urls,
            "GROK_VERIFY_TIMEOUT_S": self.verify_timeout_s,
            "GROK_VERIFY_MAILTO": self.verify_mailto or "未设置",
            "GROK_VERIFY_FAKE_IP": self.verify_fake_ip,
            "GROK_VERIFY_DOH_URL": mask_url(self.verify_doh_url),
            "GROK_PLANNING_TOOLS": self.planning_tools_enabled,
            "GROK_FETCH_MIN_CHARS": self.fetch_min_chars,
            "GROK_FETCH_MAX_CHARS": self.fetch_max_chars,
            "GROK_FETCH_SNAPSHOT_TTL_S": self.fetch_snapshot_ttl_s,
            "GROK_FETCH_MAX_CHARS_CJK": self.fetch_max_chars_cjk,
            "GROK_FETCH_ARXIV_BUDGET_S": self.fetch_arxiv_budget_s,
            "TAVILY_API_URL": mask_url(self.tavily_api_url),
            "TAVILY_ENABLED": self.tavily_enabled,
            "TAVILY_EXTRACT_TIMEOUT_S": self.tavily_extract_timeout_s,
            "TAVILY_API_KEY": self._mask_api_key(self.tavily_api_key) if self.tavily_api_key else "未配置",
            "FIRECRAWL_API_URL": mask_url(self.firecrawl_api_url),
            "FIRECRAWL_API_KEY": self._mask_api_key(self.firecrawl_api_key) if self.firecrawl_api_key else "未配置",
            "providers": self.provider_status(),
            "config_status": config_status,
        })
        warnings = self.config_warnings()
        if warnings:
            info["config_warnings"] = warnings
        return info

config = Config()
