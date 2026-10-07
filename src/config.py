"""Loader for config/comparables.yml.

Same pattern poller.py uses for markets.yml -- read the YAML, hand back plain
dicts. Kept separate so fred.py, fedwatch.py and notion_sync.py all read the
same file without importing each other.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPARABLES_CONFIG = REPO_ROOT / "config" / "comparables.yml"
DOTENV_PATH = REPO_ROOT / ".env"

_cache: dict[str, Any] | None = None
_dotenv_loaded = False


def _load_dotenv() -> None:
    """Populate os.environ from .env for any key not already set.

    CI sets secrets as real environment variables, so this is a no-op there.
    Locally (manual runs, the Hermes cron) there is no shell export step, so
    .env is the only place NOTION_API_KEY / FRED_API_KEY live -- without this,
    require_env() fails even though the key is sitting right there in the
    repo root. Never overwrites an already-set env var. No third-party
    dependency: the format here is just KEY=VALUE, one per line, # comments
    and blank lines skipped.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    if not DOTENV_PATH.exists():
        return
    for line in DOTENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load(path: Path = COMPARABLES_CONFIG) -> dict[str, Any]:
    """Parsed config/comparables.yml, memoised."""
    global _cache
    if _cache is None or path != COMPARABLES_CONFIG:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if path == COMPARABLES_CONFIG:
            _cache = data
        else:
            return data
    return _cache


def section(name: str) -> dict[str, Any]:
    return load().get(name, {}) or {}


def resolve_path(value: str) -> Path:
    """Config paths are repo-relative unless absolute."""
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


def require_env(name: str) -> str:
    """Fail loudly and early when a secret is missing."""
    _load_dotenv()
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"Missing required secret: {name}. Set it locally in your shell, "
            f"or in CI under Settings -> Secrets and variables -> Actions."
        )
    return value
