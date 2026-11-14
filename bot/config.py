"""Load config.toml (the single source of every threshold) into attribute-style namespaces."""
from __future__ import annotations

import copy
import os
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Optional

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - Python 3.9 / 3.10
    import tomli as _toml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(REPO_ROOT, "config.toml")


def _to_ns(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_ns(v) for k, v in value.items()})
    return value


def _coerce(text: str) -> Any:
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def set_dotted(data: Dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise KeyError(f"unknown config section in '{key}'")
        node = node[part]
    if parts[-1] not in node:
        raise KeyError(f"unknown config key '{key}'")
    node[parts[-1]] = _coerce(value) if isinstance(value, str) else value


def load_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None,
                set_args: Optional[Iterable[str]] = None) -> SimpleNamespace:
    """overrides: {"tender.min_ev_total": 150}; set_args: ["tender.min_ev_total=150", ...]."""
    with open(path or DEFAULT_PATH, "rb") as fh:
        data = _toml.load(fh)
    for key, value in (overrides or {}).items():
        set_dotted(data, key, value)
    for item in set_args or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"--set expects key=value, got '{item}'")
        set_dotted(data, key.strip(), value.strip())
    cfg = _to_ns(data)
    cfg._raw = copy.deepcopy(data)
    return cfg


def per_ticker(ns: Any, ticker: str, default: Any = None) -> Any:
    """Read a per-ticker table such as cfg.unwind.horizon_ticks.CRZY."""
    return getattr(ns, ticker, default)


def load_dotenv(path: Optional[str] = None) -> None:
    """Minimal .env reader (KEY=VALUE lines). Existing environment variables win."""
    path = path or os.path.join(REPO_ROOT, ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if value:
                    os.environ.setdefault(key.strip(), value)
    except OSError:
        pass
