from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def find_codex() -> str:
    configured = os.getenv("LILY_CODEX_PATH", "").strip()
    if configured:
        return configured
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    app_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    return str(app_binary) if app_binary.exists() else ""


def find_claude() -> str:
    configured = os.getenv("LILY_CLAUDE_PATH", "").strip()
    if configured:
        return configured
    return shutil.which("claude") or ""


def env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    )


def find_claude_settings() -> Path | None:
    configured = os.getenv("LILY_CLAUDE_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    config_dir = os.getenv("CLAUDE_CONFIG_DIR", "").strip()
    candidates = []
    if config_dir:
        candidates.append(Path(config_dir).expanduser() / "settings.json")
    candidates.append(Path.home() / ".claude" / "settings.json")
    return next((path for path in candidates if path.is_file()), None)


def load_claude_settings(path: Path | None) -> dict[str, Any]:
    if not path or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def claude_profile() -> dict[str, Any]:
    settings_path = find_claude_settings()
    configured = load_claude_settings(settings_path)
    config_env = configured.get("env") or {}
    if not isinstance(config_env, dict):
        config_env = {}

    def value(name: str) -> str:
        return str(os.getenv(name) or config_env.get(name) or "").strip()

    base_url = value("ANTHROPIC_BASE_URL")
    provider = os.getenv("LILY_CLAUDE_PROVIDER", "").strip()
    if not provider:
        if value("CLAUDE_CODE_USE_BEDROCK") == "1":
            provider = "Amazon Bedrock"
        elif value("CLAUDE_CODE_USE_VERTEX") == "1":
            provider = "Google Vertex AI"
        elif base_url:
            provider = urlparse(base_url).hostname or "Anthropic-compatible API"
        else:
            provider = "Anthropic / Claude account"

    model = (
        os.getenv("LILY_CLAUDE_MODEL", "").strip()
        or value("ANTHROPIC_MODEL")
        or "Claude Code default"
    )
    configured_auth = bool(
        value("ANTHROPIC_AUTH_TOKEN")
        or value("ANTHROPIC_API_KEY")
        or value("CLAUDE_CODE_USE_BEDROCK") == "1"
        or value("CLAUDE_CODE_USE_VERTEX") == "1"
    )
    source = (
        "Claude settings.json"
        if settings_path and config_env
        else "environment"
        if any(
            os.getenv(name)
            for name in (
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "LILY_CLAUDE_PROVIDER",
                "LILY_CLAUDE_MODEL",
            )
        )
        else "saved login / default"
    )
    return {
        "provider": provider,
        "model": model,
        "api_host": urlparse(base_url).hostname or "",
        "config_source": source,
        "auth_configured": configured_auth,
    }


CLAUDE_PROFILE = claude_profile()
CLAUDE_SETTINGS_PATH = find_claude_settings()


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    db_path: Path = ROOT / os.getenv("LILY_DB_PATH", "lily.db")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    codex_path: str = find_codex()
    codex_enabled: bool = env_bool("LILY_USE_CODEX", True)
    codex_timeout: int = int(os.getenv("LILY_CODEX_TIMEOUT", "900"))
    codex_env_allowlist: tuple[str, ...] = env_list("LILY_CODEX_ENV_ALLOWLIST")
    claude_path: str = find_claude()
    claude_enabled: bool = env_bool("LILY_USE_CLAUDE", True)
    claude_timeout: int = int(os.getenv("LILY_CLAUDE_TIMEOUT", "900"))
    claude_max_turns: int = int(os.getenv("LILY_CLAUDE_MAX_TURNS", "30"))
    claude_env_allowlist: tuple[str, ...] = env_list(
        "LILY_CLAUDE_ENV_ALLOWLIST"
    )
    claude_allowed_tools: tuple[str, ...] = env_list(
        "LILY_CLAUDE_ALLOWED_TOOLS",
        "Read,Glob,Grep,Edit,Write,Bash(git status:*),Bash(git diff:*)",
    )
    claude_provider: str = CLAUDE_PROFILE["provider"]
    claude_model: str = CLAUDE_PROFILE["model"]
    claude_api_host: str = CLAUDE_PROFILE["api_host"]
    claude_config_source: str = CLAUDE_PROFILE["config_source"]
    claude_auth_configured: bool = CLAUDE_PROFILE["auth_configured"]
    claude_config_path: str = (
        str(CLAUDE_SETTINGS_PATH) if CLAUDE_SETTINGS_PATH else ""
    )
    runtime_priority: tuple[str, ...] = env_list(
        "LILY_RUNTIME_PRIORITY",
        "codex-cli,claude-code",
    )
    allowed_repo_root: Path = Path(
        os.getenv("LILY_ALLOWED_REPO_ROOT", str(Path.home()))
    ).expanduser().resolve()
    worktree_root: Path = ROOT / os.getenv("LILY_WORKTREE_DIR", "lily-worktrees")
    worker_interval: float = float(os.getenv("LILY_WORKER_INTERVAL", "2"))
    heartbeat_interval: float = float(os.getenv("LILY_HEARTBEAT_INTERVAL", "10"))
    lease_timeout: float = float(os.getenv("LILY_LEASE_TIMEOUT", "60"))
    max_attempts: int = int(os.getenv("LILY_MAX_ATTEMPTS", "3"))

    def __post_init__(self) -> None:
        if self.heartbeat_interval <= 0:
            raise ValueError("LILY_HEARTBEAT_INTERVAL 必须大于 0")
        if self.lease_timeout <= self.heartbeat_interval:
            raise ValueError("LILY_LEASE_TIMEOUT 必须大于 LILY_HEARTBEAT_INTERVAL")
        if self.claude_max_turns <= 0:
            raise ValueError("LILY_CLAUDE_MAX_TURNS 必须大于 0")


settings = Settings()
