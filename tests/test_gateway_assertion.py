"""platform-api-gateway SPEC-002: only calls that came through the edge gateway (report, then enforce)."""

from __future__ import annotations

import logging
import time

import pytest
from conftest import ISSUER
from fastapi.testclient import TestClient

from solo_habits.app import create_app
from solo_habits.auth import TokenVerifier
from solo_habits.gateway import (
    MAX_BODY_BYTES,
    GatewayCheck,
    _expected,
    body_too_big,
    check,
    is_open,
)

# Test vector made by the gateway's reference (book_api_gateway.assertion.sign) from the public test
# master bytes(range(32)); it pins this copy to the gateway byte for byte.
KEY = bytes.fromhex("fe0cf5b8aad53ea81449701c218d54e663bc33d864842e739bcef8742d94a56f")
VECTOR = "v1.1793700000.cmVxLjE6YQ.FIYOR4VOYVb7_PhDWnTiFqxKyHEqUxDcs1B8dB7b0sE"
POST_VECTOR = "v1.1793700000.cmVxLjE6YQ._L2Jgjl5CrbW9qTdTfgvtGubyou3nkRZHXmFKZrxsm8"


def test_matches_the_gateway_reference():
    args = ("GET", "/v1/habits?status=active", "Bearer t")
    assert (
        _expected(
            KEY, "GET", "/v1/habits?status=active", 1793700000, "req.1:a", "Bearer t"
        )
        == VECTOR
    )
    assert check(KEY, VECTOR, *args, now=1793700000) == "valid"
    assert check(KEY, VECTOR, *args, now=1793700061) == "invalid"
    assert check(KEY, VECTOR, "POST", *args[1:], now=1793700000) == "invalid"
    assert (
        check(KEY, VECTOR, "GET", "/v1/habits", "Bearer t", now=1793700000) == "invalid"
    )
    assert check(KEY, VECTOR, "GET", args[1], "Bearer u", now=1793700000) == "invalid"
    assert check(KEY, None, *args, now=1793700000) == "missing"
    assert check(KEY, "v1.x", *args, now=1793700000) == "invalid"
    body = b'{"name":"run"}'
    post = ("POST", "/v1/habits", "Bearer t")
    assert (
        _expected(
            KEY, *post[:2], 1793700000, "req.1:a", post[2], "application/json", body
        )
        == POST_VECTOR
    )
    assert (
        check(KEY, POST_VECTOR, *post, 1793700000, "application/json", body) == "valid"
    )
    assert (
        check(
            KEY, POST_VECTOR, *post, 1793700000, "application/json", b'{"name":"swim"}'
        )
        == "invalid"
    )
    assert check(KEY, POST_VECTOR, *post, 1793700000, "text/plain", body) == "invalid"


def test_open_paths():
    assert is_open("/health")
    assert is_open("/openapi.json")
    assert is_open("/.well-known/jwks.json")
    assert not is_open("/v1/habits")
    assert not is_open("/healthz-not")
    assert not is_open("/.well-knownx")


def signed(
    method: str,
    target: str,
    authorization: str,
    content_type: str = "",
    body: bytes = b"",
) -> str:
    return _expected(
        KEY,
        method,
        target,
        int(time.time()),
        "rid-1",
        authorization,
        content_type,
        body,
    )


@pytest.fixture
def app_with(cluster, keys, platform):
    _, public = keys

    def make(mode: str) -> TestClient:
        app = create_app(
            lambda: cluster.connect("habits_api"),
            TokenVerifier(public, ISSUER),
            platform,
            gateway=GatewayCheck(mode, KEY),  # type: ignore[arg-type]
        )
        return TestClient(app)

    return make


def test_enforce_refuses_direct_calls_and_keeps_probes_open(app_with, token, owner):
    client = app_with("enforce")
    headers = token(*owner)
    direct = client.get("/v1/habits", headers=headers)
    assert direct.status_code == 403
    assert direct.json() == {"error": "gateway_required"}
    assert direct.headers["X-Correlation-Id"]
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200

    forged = {
        **headers,
        "X-Gateway-Assertion": signed("GET", "/v1/habits", "Bearer other"),
    }
    assert client.get("/v1/habits", headers=forged).status_code == 403

    through = {
        **headers,
        "X-Gateway-Assertion": signed(
            "GET", "/v1/habits?status=active", headers["Authorization"]
        ),
    }
    assert client.get("/v1/habits?status=active", headers=through).status_code == 200
    # the same assertion does not cover another query
    assert client.get("/v1/habits?status=paused", headers=through).status_code == 403


def test_enforce_binds_the_body_and_the_route_still_reads_it(app_with, token, owner):
    client = app_with("enforce")
    headers = token(*owner)
    body = b'{"name":"Run","slug":"run","category":"health","frequency":"daily"}'
    auth = headers["Authorization"]
    swapped = {
        **headers,
        "Content-Type": "application/json",
        "X-Gateway-Assertion": signed(
            "POST", "/v1/habits", auth, "application/json", b"{}"
        ),
    }
    assert client.post("/v1/habits", headers=swapped, content=body).status_code == 403
    good = {
        **headers,
        "Content-Type": "application/json",
        "X-Gateway-Assertion": signed(
            "POST", "/v1/habits", auth, "application/json", body
        ),
    }
    created = client.post("/v1/habits", headers=good, content=body)
    assert created.status_code == 201, created.text
    assert created.json()["slug"] == "run"


def test_a_body_the_gateway_could_not_send_is_never_read():
    assert not body_too_big(None, None)
    assert not body_too_big("0", None)
    assert not body_too_big(str(MAX_BODY_BYTES), None)
    assert body_too_big(str(MAX_BODY_BYTES + 1), None)
    assert body_too_big("-1", None)
    assert body_too_big("12x", None)
    assert body_too_big(None, "chunked")


def test_a_signed_header_with_a_big_body_is_refused_unread(
    app_with, token, owner, monkeypatch
):
    from starlette.requests import Request

    reads = []
    original = Request.body

    async def counting_body(self):
        reads.append(1)
        return await original(self)

    monkeypatch.setattr(Request, "body", counting_body)
    client = app_with("enforce")
    headers = token(*owner)
    body = b"x" * (MAX_BODY_BYTES + 1)
    big = {
        **headers,
        "Content-Type": "application/json",
        "X-Gateway-Assertion": signed(
            "POST", "/v1/habits", headers["Authorization"], "application/json", body
        ),
    }
    assert client.post("/v1/habits", headers=big, content=body).status_code == 403
    assert reads == []


def test_report_serves_and_logs_without_secrets(app_with, token, owner, caplog):
    client = app_with("report")
    headers = token(*owner)
    with caplog.at_level(logging.INFO, logger="solo_habits.gateway"):
        assert client.get("/health").status_code == 200  # open paths are not reported
        assert (
            client.get("/v1/habits?status=active", headers=headers).status_code == 200
        )
        ok = {
            **headers,
            "X-Gateway-Assertion": signed(
                "GET", "/v1/habits", headers["Authorization"]
            ),
        }
        assert client.get("/v1/habits", headers=ok).status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "solo_habits.gateway"]
    assert lines == [
        "gateway_assertion result=missing method=GET path=/v1/habits",
        "gateway_assertion result=valid method=GET path=/v1/habits",
    ]
    text = "\n".join(lines)
    assert "Bearer" not in text
    assert "status=active" not in text


def test_from_env(monkeypatch):
    monkeypatch.delenv("GATEWAY_ASSERTION_MODE", raising=False)
    assert GatewayCheck.from_env() is None
    monkeypatch.setenv("GATEWAY_ASSERTION_MODE", "report")
    monkeypatch.setenv("GATEWAY_ASSERTION_KEY", "ab" * 31)
    with pytest.raises(SystemExit):
        GatewayCheck.from_env()
    monkeypatch.setenv("GATEWAY_ASSERTION_KEY", KEY.hex())
    assert GatewayCheck.from_env() == GatewayCheck("report", KEY)
    monkeypatch.setenv("GATEWAY_ASSERTION_MODE", "strict")
    with pytest.raises(SystemExit):
        GatewayCheck.from_env()


def test_refused_before_the_body_is_read(app_with, token, owner, monkeypatch):
    from starlette.requests import Request

    reads = []
    original = Request.body

    async def counting(self):
        reads.append(1)
        return await original(self)

    monkeypatch.setattr(Request, "body", counting)
    client = app_with("enforce")
    headers = {**token(*owner), "Content-Type": "application/json"}
    for header in (None, "v1.x", "v1.1.YQ." + "a" * 43):  # missing, malformed, stale
        h = (
            dict(headers)
            if header is None
            else {**headers, "X-Gateway-Assertion": header}
        )
        assert client.post("/v1/habits", headers=h, content=b"{}").status_code == 403
    assert reads == []


def test_probe_paths_and_a_trailing_slash_stay_open():
    from solo_habits.gateway import is_open

    for path in (
        "/health",
        "/healthz",
        "/ready",
        "/readyz",
        "/health/",
        "/.well-known/jwks.json",
    ):
        assert is_open(path), path
    for path in ("/healthcheck", "/ready-now", "/v1/health"):
        assert not is_open(path), path


def test_a_stalled_body_is_answered_408_and_not_waited_on(app_with, monkeypatch):
    import asyncio

    from solo_habits import app as app_module

    monkeypatch.setattr(app_module, "BODY_TIMEOUT_SECONDS", 0.2)
    app = app_with("report").app
    header = signed("POST", "/v1/habits", "")
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        await asyncio.sleep(5)  # the client never sends its body
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/habits",
        "raw_path": b"/v1/habits",
        "query_string": b"",
        "headers": [
            (b"x-gateway-assertion", header.encode()),
            (b"content-length", b"10"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    asyncio.run(asyncio.wait_for(app(scope, receive, send), 3))
    assert sent[0]["status"] == 408
