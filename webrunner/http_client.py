"""A very small HTTP client built on urllib.

The whole point of WebRunner is to run other people's code with few moving
parts, so the HTTP block avoids pulling in a third-party client. Requests are
executed on a worker thread so they never block the scheduler loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = "WebRunner/1.0 (+https://github.com/yourname/webrunner)"
MAX_BODY_BYTES = 512 * 1024


@dataclass
class HttpResult:
    ok: bool = False
    status_code: int = 0
    reason: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    json: Any = None
    elapsed: float = 0.0
    error: str = ""
    url: str = ""
    truncated: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _parse_status_rule(rule: str) -> Any:
    """Turn '2xx', 'any', '200,201' or '200-299' into a predicate."""
    rule = (rule or "2xx").strip().lower()
    if rule in ("any", "*", ""):
        return lambda _code: True
    checks: list[Any] = []
    for piece in rule.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if piece.endswith("xx") and piece[:-2].isdigit():
            hundreds = int(piece[:-2])
            checks.append(lambda code, h=hundreds: code // 100 == h)
        elif "-" in piece:
            low_raw, _, high_raw = piece.partition("-")
            try:
                low, high = int(low_raw), int(high_raw)
            except ValueError:
                continue
            checks.append(lambda code, lo=low, hi=high: lo <= code <= hi)
        elif piece.isdigit():
            checks.append(lambda code, want=int(piece): code == want)
    if not checks:
        return lambda code: code // 100 == 2
    return lambda code: any(check(code) for check in checks)


def request_sync(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    verify_tls: bool = True,
    expect_status: str = "2xx",
) -> HttpResult:
    result = HttpResult(url=url)
    method = (method or "GET").upper()

    if params:
        parts = urllib.parse.urlsplit(url)
        merged = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        merged.extend((key, value) for key, value in params.items() if key)
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(merged)))
        result.url = url

    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        result.error = f"unsupported URL scheme {scheme!r}"
        return result

    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        if key:
            request.add_header(key, value)

    handlers: list[Any] = []
    if not follow_redirects:
        handlers.append(_NoRedirect())
    if scheme == "https":
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)

    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(MAX_BODY_BYTES + 1)
            result.status_code = response.status
            result.reason = response.reason or ""
            result.headers = {key.title(): value for key, value in response.headers.items()}
            _fill_body(result, payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read(MAX_BODY_BYTES + 1) if exc.fp else b""
        result.status_code = exc.code
        result.reason = exc.reason or ""
        result.headers = {key.title(): value for key, value in (exc.headers or {}).items()}
        _fill_body(result, payload)
    except urllib.error.URLError as exc:
        result.error = f"{type(exc.reason).__name__ if exc.reason else 'URLError'}: {exc.reason}"
    except TimeoutError:
        result.error = f"request timed out after {timeout:g}s"
    except (OSError, ValueError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"

    result.elapsed = time.monotonic() - started
    if not result.error:
        result.ok = _parse_status_rule(expect_status)(result.status_code)
        if not result.ok:
            result.error = f"unexpected status {result.status_code} (wanted {expect_status})"
    return result


def _fill_body(result: HttpResult, payload: bytes) -> None:
    if len(payload) > MAX_BODY_BYTES:
        payload = payload[:MAX_BODY_BYTES]
        result.truncated = True
    charset = "utf-8"
    content_type = result.headers.get("Content-Type", "")
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        result.body = payload.decode(charset, errors="replace")
    except LookupError:
        result.body = payload.decode("utf-8", errors="replace")
    stripped = result.body.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            result.json = json.loads(result.body)
        except ValueError:
            result.json = None


async def request(*args: Any, **kwargs: Any) -> HttpResult:
    return await asyncio.to_thread(request_sync, *args, **kwargs)


def basic_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"
