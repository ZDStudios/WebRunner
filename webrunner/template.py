"""The tiny ``{{ ... }}`` template language used across blocks.

    {{ vars.target }}
    {{ steps.fetch.json.data.0.id }}
    {{ secrets.API_KEY }}
    {{ vars.name ?? "world" }}
    {{ steps.fetch.stdout | trim | json }}

Resolution is forgiving on purpose: an unknown path renders as an empty string
rather than exploding a scheduled job at 3am. Use ``?? fallback`` when you want
something more useful than empty.

Available filters: json, upper, lower, trim, urlencode, b64, int, len, first,
last, lines, replace:a:b, slice:n.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

_PATTERN = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def resolve(path: str, context: dict[str, Any]) -> Any:
    """Walk a dotted path through nested dicts and lists."""
    current: Any = context
    for part in path.split("."):
        part = part.strip()
        if not part:
            return None
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def stringify(value: Any) -> str:
    """Render a value the way it appears inside a template."""
    return _stringify(value)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _apply_filter(value: Any, name: str, args: list[str]) -> Any:
    name = name.strip()
    if name == "json":
        return json.dumps(value, ensure_ascii=False, default=str)
    if name == "upper":
        return _stringify(value).upper()
    if name == "lower":
        return _stringify(value).lower()
    if name == "trim":
        return _stringify(value).strip()
    if name == "urlencode":
        return urllib.parse.quote(_stringify(value), safe="")
    if name == "b64":
        return base64.b64encode(_stringify(value).encode("utf-8")).decode("ascii")
    if name == "int":
        try:
            return int(float(_stringify(value) or 0))
        except ValueError:
            return 0
    if name == "len":
        try:
            return len(value)  # type: ignore[arg-type]
        except TypeError:
            return len(_stringify(value))
    if name == "first":
        if isinstance(value, (list, tuple)) and value:
            return value[0]
        return _stringify(value).splitlines()[0] if _stringify(value) else ""
    if name == "last":
        if isinstance(value, (list, tuple)) and value:
            return value[-1]
        lines = _stringify(value).splitlines()
        return lines[-1] if lines else ""
    if name == "lines":
        return _stringify(value).splitlines()
    if name == "replace" and len(args) >= 2:
        return _stringify(value).replace(args[0], args[1])
    if name == "slice" and args:
        try:
            return _stringify(value)[: int(args[0])]
        except ValueError:
            return value
    if name == "default":
        text = _stringify(value)
        return text if text else (args[0] if args else "")
    return value


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _split_filters(expression: str) -> list[str]:
    """Split on single ``|`` while leaving ``||`` alone."""
    parts, buffer, index = [], [], 0
    while index < len(expression):
        char = expression[index]
        if char == "|":
            if index + 1 < len(expression) and expression[index + 1] == "|":
                buffer.append("||")
                index += 2
                continue
            parts.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return parts


def evaluate(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate one ``{{ ... }}`` body and return the raw (untyped) value."""
    segments = _split_filters(expression)
    head = segments[0].strip()
    fallback: str | None = None

    if "??" in head:
        head, _, raw_fallback = head.partition("??")
        fallback = _unquote(raw_fallback)
        head = head.strip()

    if (head.startswith('"') and head.endswith('"')) or (head.startswith("'") and head.endswith("'")):
        value: Any = _unquote(head)
    else:
        value = resolve(head, context)

    if value in (None, "") and fallback is not None:
        value = fallback

    for segment in segments[1:]:
        pieces = segment.strip().split(":")
        value = _apply_filter(value, pieces[0], [_unquote(p) for p in pieces[1:]])
    return value


def render(text: Any, context: dict[str, Any]) -> str:
    """Render every placeholder in ``text``."""
    if text is None:
        return ""
    if not isinstance(text, str):
        return _stringify(text)
    if "{{" not in text:
        return text
    return _PATTERN.sub(lambda match: _stringify(evaluate(match.group(1), context)), text)


def render_value(value: Any, context: dict[str, Any]) -> Any:
    """Render recursively through dicts and lists, leaving non-strings alone."""
    if isinstance(value, str):
        return render(value, context)
    if isinstance(value, dict):
        return {key: render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    return value


def base_context(job: dict | None = None, run_id: int | None = None) -> dict[str, Any]:
    now = datetime.now().astimezone()
    return {
        "vars": {},
        "secrets": {},
        "steps": {},
        "env": {},
        "job": {
            "id": (job or {}).get("id", 0),
            "name": (job or {}).get("name", ""),
            "description": (job or {}).get("description", ""),
            "tags": (job or {}).get("tags", ""),
        },
        "run": {"id": run_id or 0},
        "now": {
            "iso": now.isoformat(timespec="seconds"),
            "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "year": now.strftime("%Y"),
            "month": now.strftime("%m"),
            "day": now.strftime("%d"),
            "hour": now.strftime("%H"),
            "minute": now.strftime("%M"),
            "weekday": now.strftime("%A"),
            "epoch": int(now.timestamp()),
        },
        "last": {},
    }
