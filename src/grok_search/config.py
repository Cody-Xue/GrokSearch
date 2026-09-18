import os
import json
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


class Config:
    _instance = None
    _SETUP_COMMAND = (
        'claude mcp add-json grok-search --scope user '
        '\'{"type":"stdio","command":"uvx","args":["--from",'
        '"git+https://github.com/GuDaStudio/GrokSearch","grok-search"],'
        '"env":{"GUDA_API_KEY":"your-guda-api-key"}}\''
    )
    _DEFAULT_MODEL = "grok-4.20-beta"
    _DEFAULT_GUDA_BASE_URL = "https://code.guda.studio"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config_file = None
            cls._instance._cached_model = None
        return cls._instance

    @property
    def config_file(self) -> Path:
        if self._config_file is None:
            config_dir = Path.home() / ".config" / "grok-search"
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                config_dir = Path.cwd() / ".grok-search"
                config_dir.mkdir(parents=True, exist_ok=True)
            self._config_file = config_dir / "config.json"
        return self._config_file

    def _load_config_file(self) -> dict:
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_config_file(self, config_data: dict) -> None:
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            raise ValueError(f"无法保存配置文件: {str(e)}")

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

    # -------------------------------------------------------- throttling / breaker
    @property
    def max_concurrency(self) -> int:
        return max(1, _env_int("GROK_MAX_CONCURRENCY", 4))

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
        style = os.getenv("GROK_SEARCH_STYLE", "explanatory").strip().lower()
        return style if style in ("explanatory", "concise") else "explanatory"

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
    def tavily_extract_timeout_s(self) -> float:
        return max(5.0, _env_float("TAVILY_EXTRACT_TIMEOUT_S", 30.0))

    # ------------------------------------------------------------- endpoints
    @property
    def guda_base_url(self) -> str:
        return os.getenv("GUDA_BASE_URL", self._DEFAULT_GUDA_BASE_URL)

    @property
    def guda_api_key(self) -> str | None:
        return os.getenv("GUDA_API_KEY")

    @property
    def grok_api_url(self) -> str:
        url = os.getenv("GROK_API_URL")
        if not url:
            if self.guda_api_key:
                return f"{self.guda_base_url}/grok/v1"
            raise ValueError(
                f"Grok API URL 未配置！\n"
                f"请使用以下命令配置 MCP 服务器：\n{self._SETUP_COMMAND}"
            )
        return url

    @property
    def grok_api_key(self) -> str:
        key = os.getenv("GROK_API_KEY") or self.guda_api_key
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
        url = os.getenv("TAVILY_API_URL")
        if not url and self.guda_api_key:
            return f"{self.guda_base_url}/tavily"
        return url or "https://api.tavily.com"

    @property
    def tavily_api_key(self) -> str | None:
        return os.getenv("TAVILY_API_KEY") or self.guda_api_key

    @property
    def firecrawl_api_url(self) -> str:
        url = os.getenv("FIRECRAWL_API_URL")
        if not url and self.guda_api_key:
            return f"{self.guda_base_url}/firecrawl"
        return url or "https://api.firecrawl.dev/v2"

    @property
    def firecrawl_api_key(self) -> str | None:
        return os.getenv("FIRECRAWL_API_KEY") or self.guda_api_key

    @property
    def log_level(self) -> str:
        return os.getenv("GROK_LOG_LEVEL", "INFO").upper()

    @property
    def log_dir(self) -> Path:
        log_dir_str = os.getenv("GROK_LOG_DIR", "logs")
        log_dir = Path(log_dir_str)
        if log_dir.is_absolute():
            return log_dir

        home_log_dir = Path.home() / ".config" / "grok-search" / log_dir_str
        try:
            home_log_dir.mkdir(parents=True, exist_ok=True)
            return home_log_dir
        except OSError:
            pass

        cwd_log_dir = Path.cwd() / log_dir_str
        try:
            cwd_log_dir.mkdir(parents=True, exist_ok=True)
            return cwd_log_dir
        except OSError:
            pass

        tmp_log_dir = Path("/tmp") / "grok-search" / log_dir_str
        tmp_log_dir.mkdir(parents=True, exist_ok=True)
        return tmp_log_dir

    # ------------------------------------------------------------------ model
    def _apply_model_suffix(self, model: str) -> str:
        try:
            url = self.grok_api_url
        except ValueError:
            return model
        if "openrouter" in url and ":online" not in model:
            return f"{model}:online"
        return model

    @property
    def grok_model_source(self) -> str:
        """Where the effective default model comes from: env, config file or default."""
        if os.getenv("GROK_MODEL"):
            return "env"
        if self._load_config_file().get("model"):
            return "config"
        return "default"

    @property
    def grok_model(self) -> str:
        if self._cached_model is not None:
            return self._cached_model

        model = (
            os.getenv("GROK_MODEL")
            or self._load_config_file().get("model")
            or self._DEFAULT_MODEL
        )
        self._cached_model = self._apply_model_suffix(model)
        return self._cached_model

    def set_model(self, model: str) -> None:
        config_data = self._load_config_file()
        config_data["model"] = model
        self._save_config_file(config_data)
        self._cached_model = self._apply_model_suffix(model)

    @staticmethod
    def _mask_api_key(key: str) -> str:
        """脱敏显示 API Key，只显示前后各 4 个字符"""
        if not key or len(key) <= 8:
            return "***"
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    def get_config_info(self) -> dict:
        """获取配置信息（API Key 已脱敏）"""
        try:
            api_url = self.grok_api_url
            api_key_raw = self.grok_api_key
            api_key_masked = self._mask_api_key(api_key_raw)
            config_status = "✅ 配置完整"
        except ValueError as e:
            api_url = "未配置"
            api_key_masked = "未配置"
            config_status = f"❌ 配置错误: {str(e)}"

        info = {
            "GUDA_BASE_URL": self.guda_base_url,
            "GUDA_API_KEY": self._mask_api_key(self.guda_api_key) if self.guda_api_key else "未配置",
            "GROK_API_URL": api_url,
            "GROK_API_KEY": api_key_masked,
            "GROK_MODEL": self.grok_model,
            "GROK_MODEL_SOURCE": self.grok_model_source,
            "GROK_DEBUG": self.debug_enabled,
            "GROK_LOG_LEVEL": self.log_level,
            "GROK_LOG_DIR": str(self.log_dir),
            "GROK_SEARCH_STYLE": self.search_style,
            "GROK_MAX_CONCURRENCY": self.max_concurrency,
            "GROK_RETRY_MAX_ATTEMPTS": self.retry_max_attempts,
            "GROK_RETRY_BUDGET_S": self.retry_budget_s,
            "GROK_BREAKER": {
                "threshold": self.breaker_threshold,
                "window_s": self.breaker_window_s,
                "cooldown_s": self.breaker_cooldown_s,
                "max_cooldown_s": self.breaker_max_cooldown_s,
            },
            "GROK_VERIFY_IDS": self.verify_ids,
            "GROK_VERIFY_URLS": self.verify_urls,
            "GROK_PLANNING_TOOLS": self.planning_tools_enabled,
            "GROK_FETCH_MIN_CHARS": self.fetch_min_chars,
            "GROK_FETCH_MAX_CHARS": self.fetch_max_chars,
            "TAVILY_API_URL": self.tavily_api_url,
            "TAVILY_ENABLED": self.tavily_enabled,
            "TAVILY_EXTRACT_TIMEOUT_S": self.tavily_extract_timeout_s,
            "TAVILY_API_KEY": self._mask_api_key(self.tavily_api_key) if self.tavily_api_key else "未配置",
            "FIRECRAWL_API_URL": self.firecrawl_api_url,
            "FIRECRAWL_API_KEY": self._mask_api_key(self.firecrawl_api_key) if self.firecrawl_api_key else "未配置",
            "config_status": config_status,
        }
        return info

config = Config()
