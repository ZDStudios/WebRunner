"""Runtime configuration, resolved from environment variables with sane defaults."""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")


def _default_data_dir() -> Path:
    override = os.environ.get("WEBRUNNER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        base = os.environ.get("PROGRAMDATA") or os.path.expanduser("~")
        return Path(base) / "WebRunner"
    # A packaged install writes to /var/lib; a plain `pip install --user` run
    # falls back to the XDG location so it never needs root.
    system = Path("/var/lib/webrunner")
    if os.access(system.parent, os.W_OK) or system.exists():
        return system
    return Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "webrunner"


def _default_shell() -> str:
    if IS_WINDOWS:
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL") or "/bin/sh"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Config:
    data_dir: Path = field(default_factory=_default_data_dir)
    host: str = field(default_factory=lambda: os.environ.get("WEBRUNNER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("WEBRUNNER_PORT", 8770))
    base_url: str = field(default_factory=lambda: os.environ.get("WEBRUNNER_BASE_URL", "").rstrip("/"))

    # Auth
    auth_disabled: bool = field(default_factory=lambda: _env_bool("WEBRUNNER_AUTH_DISABLED"))
    admin_user: str = field(default_factory=lambda: os.environ.get("WEBRUNNER_ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: os.environ.get("WEBRUNNER_ADMIN_PASSWORD", ""))
    session_days: int = field(default_factory=lambda: _env_int("WEBRUNNER_SESSION_DAYS", 14))
    secure_cookie: bool = field(default_factory=lambda: _env_bool("WEBRUNNER_SECURE_COOKIE"))

    # Execution
    default_shell: str = field(default_factory=lambda: os.environ.get("WEBRUNNER_SHELL", _default_shell()))
    timezone: str = field(default_factory=lambda: os.environ.get("WEBRUNNER_TZ") or os.environ.get("TZ") or "UTC")
    max_output_bytes: int = field(default_factory=lambda: _env_int("WEBRUNNER_MAX_OUTPUT", 512 * 1024))
    tick_seconds: float = field(default_factory=lambda: float(os.environ.get("WEBRUNNER_TICK", "1.0")))
    max_queued_per_job: int = field(default_factory=lambda: _env_int("WEBRUNNER_MAX_QUEUED", 25))
    history_limit: int = field(default_factory=lambda: _env_int("WEBRUNNER_HISTORY_LIMIT", 5000))

    log_level: str = field(default_factory=lambda: os.environ.get("WEBRUNNER_LOG_LEVEL", "info"))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "webrunner.db"

    @property
    def secret_key(self) -> str:
        """Signing key for sessions; generated once and persisted at 0600."""
        env = os.environ.get("WEBRUNNER_SECRET")
        if env:
            return env
        key_file = self.data_dir / "secret.key"
        if key_file.exists():
            value = key_file.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        key_file.write_text(value, encoding="utf-8")
        try:
            key_file.chmod(0o600)
        except OSError:  # pragma: no cover - best effort on exotic filesystems
            pass
        return value


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def set_config(cfg: Config) -> None:
    global _config
    _config = cfg
