"""Accept calls that came through the edge gateway (platform-api-gateway SPEC-002).

The gateway adds ``X-Gateway-Assertion``: an HMAC with this service's own key over the system id,
method, request target, issue time, request id and the ``Authorization`` value. ``report`` mode logs
whether a request carried a valid one (method, target, token, content type and body unchanged); ``enforce`` answers 403 to one that did not. The check is a
copy of the gateway's reference (cells own their code), kept byte-compatible by the tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Literal

SYSTEM = "habits"  # this service's id in the gateway route table
HEADER = "x-gateway-assertion"
MAX_SKEW_SECONDS = 60
# Platform probes and describe_cell stay reachable without the gateway (the platform-* services probe
# /healthz and /readyz; some cells answer /ready).
OPEN_PATHS = (
    "/health",
    "/healthz",
    "/ready",
    "/readyz",
    "/openapi.json",
    "/.well-known/",
)
# A body the header says to wait for must arrive within this, or the request is "invalid" (a client that
# sends a well-formed header and then stalls cannot hold the connection).
BODY_TIMEOUT_SECONDS = 30
# The gateway refuses bodies above 1 MiB (MAX_BODY_BYTES) and forwards a Content-Length, so a bigger or
# length-less (chunked) body cannot carry a valid assertion: it is "invalid" without being read.
MAX_BODY_BYTES = 1_048_576
_HEADER_RE = re.compile(
    r"v1\.([0-9]{1,12})\.([A-Za-z0-9_-]{1,180})\.([A-Za-z0-9_-]{43})"
)

Mode = Literal["off", "report", "enforce"]
log = logging.getLogger("solo_habits.gateway")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sha(value: bytes | str) -> str:
    data = (
        value if isinstance(value, bytes) else value.encode("utf-8", "surrogateescape")
    )
    return hashlib.sha256(data).hexdigest()


def _expected(
    key: bytes,
    method: str,
    target: str,
    iat: int,
    request_id: str,
    authorization: str,
    content_type: str = "",
    body: bytes = b"",
) -> str:
    fields = (
        "gw1",
        SYSTEM,
        method.upper(),
        target,
        str(iat),
        request_id,
        _sha(authorization),
        _sha(content_type),
        _sha(body),
    )
    text = "\n".join(fields)
    mac = hmac.new(
        key, text.encode("utf-8", "surrogateescape"), hashlib.sha256
    ).digest()
    return f"v1.{iat}.{_b64(request_id.encode())}.{_b64(mac)}"


def check(
    key: bytes,
    header: str | None,
    method: str,
    target: str,
    authorization: str,
    now: float,
    content_type: str = "",
    body: bytes = b"",
) -> str:
    """'valid', 'missing' or 'invalid' (malformed, stale or a wrong MAC)."""
    if not header:
        return "missing"
    found = _HEADER_RE.fullmatch(header.strip())
    if not found:
        return "invalid"
    try:
        request_id = base64.urlsafe_b64decode(
            found[2] + "=" * (-len(found[2]) % 4)
        ).decode()
    except (ValueError, UnicodeDecodeError):
        return "invalid"
    iat = int(found[1])
    if abs(now - iat) > MAX_SKEW_SECONDS:
        return "invalid"
    expected = _expected(
        key, method, target, iat, request_id, authorization, content_type, body
    )
    return (
        "valid"
        if hmac.compare_digest(expected.encode(), header.strip().encode())
        else "invalid"
    )


def precheck(header: str | None, now: float) -> str | None:
    """'missing' or 'invalid' when the header alone settles it (no body read); None = needs the MAC."""
    if not header:
        return "missing"
    found = _HEADER_RE.fullmatch(header.strip())
    if not found or abs(now - int(found[1])) > MAX_SKEW_SECONDS:
        return "invalid"
    return None


def body_too_big(content_length: str | None, transfer_encoding: str | None) -> bool:
    """True when the body is not one the gateway could have sent (so it is never buffered)."""
    if transfer_encoding:
        return True
    if content_length is None:
        return False  # no body
    try:
        return not 0 <= int(content_length) <= MAX_BODY_BYTES
    except ValueError:
        return True


@dataclass(frozen=True)
class GatewayCheck:
    mode: Mode
    key: bytes

    @classmethod
    def from_env(cls) -> GatewayCheck | None:
        mode = os.environ.get("GATEWAY_ASSERTION_MODE", "off").strip() or "off"
        if mode == "off":
            return None
        if mode not in ("report", "enforce"):
            raise SystemExit("GATEWAY_ASSERTION_MODE must be off, report or enforce")
        raw = os.environ.get("GATEWAY_ASSERTION_KEY", "").strip()
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = b""
        if len(key) != 32:
            raise SystemExit(
                "GATEWAY_ASSERTION_KEY must be 64 hex characters when the check is on"
            )
        if not log.handlers:  # nothing else configures logging here; stdout keeps Railway from tagging errors
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(handler)
            log.setLevel(logging.INFO)
            log.propagate = False
        return cls(mode, key)  # type: ignore[arg-type]

    def result(
        self,
        method: str,
        raw_path: str,
        query: str,
        header: str | None,
        authorization: str,
        content_type: str,
        body: bytes,
    ) -> str:
        target = raw_path + (f"?{query}" if query else "")
        return check(
            self.key,
            header,
            method,
            target,
            authorization,
            time.time(),
            content_type,
            body,
        )

    def refuses(self, path: str, result: str) -> bool:
        return self.mode == "enforce" and result != "valid" and not is_open(path)


def is_open(path: str) -> bool:
    # One trailing slash is ignored, as the routers do (/health/ is /health).
    path = path[:-1] if len(path) > 1 and path.endswith("/") else path
    return any(
        path == p or (p.endswith("/") and (path + "/").startswith(p))
        for p in OPEN_PATHS
    )


def report(result: str, method: str, path: str) -> None:
    # The route family only (/v1/habits, /v1/routines…): never the token, query or header value.
    family = "/".join(path.split("/")[:3])[:64]
    log.info("gateway_assertion result=%s method=%s path=%s", result, method, family)
