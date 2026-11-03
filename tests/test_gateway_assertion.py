"""platform-api-gateway SPEC-002: only calls that came through the edge gateway (report, then enforce)."""

from __future__ import annotations

import logging
import time

import pytest
from conftest import ISSUER
from fastapi.testclient import TestClient

from solo_habits.app import create_app
from solo_habits.auth import TokenVerifier
from solo_habits.gateway import GatewayCheck, _expected, check, is_open

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
