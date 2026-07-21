import os
import shutil
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    db_path: Path = ROOT / os.getenv("LILY_DB_PATH", "lily.db")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    codex_path: str = find_codex()
    codex_enabled: bool = env_bool("LILY_USE_CODEX", True)
    codex_timeout: int = int(os.getenv("LILY_CODEX_TIMEOUT", "900"))
    codex_env_allowlist: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv("LILY_CODEX_ENV_ALLOWLIST", "").split(",")
        if item.strip()
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


settings = Settings()
