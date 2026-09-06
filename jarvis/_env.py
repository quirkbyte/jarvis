"""
The machinery that binds environment variables onto the config tree, kept apart
from `config.py` so that file stays a flat declaration of what JARVIS's knobs
*are* rather than a mix of that and how they get filled.

Two rules live here. A malformed override fails loudly at startup, because a
`JARVIS_VAD_SILENCE_MS=soon` that silently keeps the default is a bug you find
at three in the morning by ear. And `.env` never overrides a real shell
variable, so `JARVIS_VERBOSE=1 make run` beats whatever the file says.
"""

from __future__ import annotations

import os
from dataclasses import Field, fields, is_dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

REPO_ROOT = Path(__file__).resolve().parent.parent

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


class ConfigError(ValueError):
    """A JARVIS_* environment variable that cannot be parsed into its field."""


def _coerce(raw: str, typ: Any, name: str) -> Any:
    origin = get_origin(typ)
    if origin in (Union, UnionType):
        args = [a for a in get_args(typ) if a is not type(None)]
        if raw.strip() == "" or raw.strip().lower() in {"none", "null"}:
            return None
        return _coerce(raw, args[0], name)
    if origin is tuple:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if typ is bool:
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"{name}={raw!r} is not a boolean")
    try:
        if typ is int:
            return int(raw)
        if typ is float:
            return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a {typ.__name__}") from exc
    if typ is Path:
        return Path(raw).expanduser()
    return raw


def _env_names(f: Field[Any], prefix: str) -> list[str]:
    names = [f"{prefix}{f.name.upper()}"]
    alias = f.metadata.get("env")
    if alias:
        names.append(alias)
    return names


def bind(cls: type, prefix: str, env: dict[str, str]) -> Any:
    """Construct ``cls``, filling each field from ``prefix``+FIELD or its alias."""
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        typ = hints[f.name]
        if is_dataclass(typ):
            kwargs[f.name] = bind(typ, f"{prefix}{f.name.upper()}_", env)
            continue
        for name in _env_names(f, prefix):
            if name in env:
                kwargs[f.name] = _coerce(env[name], typ, name)
                break
    return cls(**kwargs)


def describe(obj: Any) -> dict[str, Any]:
    """The tree as plain JSON-able data, with secrets reduced to whether they are set."""
    out: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        if is_dataclass(value):
            out[f.name] = describe(value)
        elif f.metadata.get("secret"):
            out[f.name] = "<set>" if value else None
        else:
            out[f.name] = str(value) if isinstance(value, Path) else value
    return out


def load_dotenv(path: Path | None = None, env: dict[str, str] | None = None) -> int:
    """Fill ``env`` from a .env file without ever overriding what is already set.

    Hand-rolled rather than a dependency: the format we need is KEY=value and
    nothing else, and BUILD.md's stack list is a closed set.
    """
    target = os.environ if env is None else env
    path = path or REPO_ROOT / ".env"
    if not path.is_file():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and key not in target:
            target[key] = value
            loaded += 1
    return loaded
