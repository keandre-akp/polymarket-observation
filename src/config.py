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

_cache: dict[str, Any] | None = None


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
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"Missing required secret: {name}. Set it locally in your shell, "
            f"or in CI under Settings -> Secrets and variables -> Actions."
        )
    return value
