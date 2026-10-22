"""MCP server tests: fake HTTP transport, no database and no network."""

import asyncio

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from solo_habits import mcp_server as mod
from solo_habits.mcp_server import build_server

API_KEY = "EMPIRE_HABITS_API"
READS = {
    "habits.habit.list",
    "habits.habit.get",
    "habits.habit.streaks",
    "habits.habit.stats",
    "habits.habit.logs",
    "habits.routine.list",
    "habits.routine.get",
    "habits.routine.stats",
    "habits.routine.logs",
}
WRITES = {"habits.habit.log", "habits.routine.log"}
ARGS = {
    "habits.habit.get": {"id": 1},
    "habits.routine.get": {"id": 2},
    "habits.habit.log": {"habit_id": 1, "log_date": "2026-10-12", "completed": True},
    "habits.routine.log": {"routine_id": 2, "log_date": "2026-10-12", "mood_after": 4},
}
SAMPLES = [
    (
        "habits.habit.list",
        {"status": ["active"], "limit": 5},
        "GET",
        "/v1/habits",
        {"status": "active", "limit": "5", "offset": "0"},
    ),
    ("habits.habit.get", {"id": 7}, "GET", "/v1/habits/7", {}),
    (
        "habits.habit.logs",
        {"habit_id": 7, "date_from": "2026-10-01"},
        "GET",
        "/v1/habit-logs",
        {
            "habit_id": "7",
            "from": "2026-10-01",
            "order": "desc",
            "limit": "100",
            "offset": "0",
        },
    ),
    (
        "habits.habit.log",
        {"habit_id": 7, "log_date": "2026-10-12", "count": 2, "if_absent": True},
        "PUT",
        "/v1/habits/7/logs/2026-10-12",
        {"if_absent": "true"},
    ),
]


def test_streaks_projects_only_streak_fields():
    fake = Fake(
        body={
            "items": [
                {
                    "id": 1,
                    "name": "run",
                    "slug": "run",
                    "status": "active",
                    "current_streak": 3,
                    "best_streak": 9,
                    "last_completed_at": None,
                    "notes": "private",
                }
            ]
        }
    )
    out = call(server(fake), "habits.habit.streaks")
    text = out.content[0].text
    assert '"current_streak": 3' in text
    assert "private" not in text


IDENTITY = {
    "actor": "x",
    "workspace_id": "w",
    "workspace": "w",
    "tenant_id": "t",
    "delegation": "d",
    "token": "t",
    "authorization": "Bearer x",
    "sub": "s",
    "actor_id": "a",
    "tenant": "t",
    "owner_id": "o",
}


class Fake:
    """A fake HTTP transport: records every request and answers with ``body``."""

    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status, self.body = status, {"items": []} if body is None else body

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, json=self.body)

        return httpx.Client(
            transport=httpx.MockTransport(handle), base_url="https://api.test"
        )


def server(fake: Fake, read_only: bool = False):
    return build_server(fake.client(), lambda: "tok-123", read_only=read_only).server


def call(srv, name: str, args: dict | None = None):
    return asyncio.run(srv.call_tool(name, args or {}))


def names(srv) -> set[str]:
    return {tool.name for tool in asyncio.run(srv.list_tools())}


def test_tools_list_matches_the_contract():
    srv = server(Fake())
    assert names(srv) == READS | WRITES
    for tool in asyncio.run(srv.list_tools()):
        assert tool.input_schema["additionalProperties"] is False


def test_read_only_registers_only_read_tools():
    assert names(server(Fake(), read_only=True)) == READS


def test_unknown_arguments_are_rejected_before_any_http_call():
    fake = Fake()
    srv = server(fake)
    for name in sorted(READS | WRITES):
        with pytest.raises(Exception, match="invalid_arguments"):
            call(srv, name, {**ARGS.get(name, {}), "surprise": 1})
    assert fake.requests == []


def test_identity_arguments_are_never_accepted():
    fake = Fake()
    srv = server(fake)
    for name in sorted(READS | WRITES):
        for key, value in IDENTITY.items():
            with pytest.raises(Exception, match="invalid_arguments"):
                call(srv, name, {**ARGS.get(name, {}), key: value})
    assert fake.requests == []
    for name in READS | WRITES:
        tool = next(t for t in asyncio.run(srv.list_tools()) if t.name == name)
        assert not set(IDENTITY) & set(tool.input_schema["properties"])


def test_every_call_carries_the_callers_bearer_and_nothing_else_of_identity():
    fake = Fake()
    srv = server(fake)
    for name in sorted(READS | WRITES):
        call(srv, name, ARGS.get(name, {}))
    assert len(fake.requests) == len(READS | WRITES)
    for request in fake.requests:
        assert request.headers["authorization"] == "Bearer tok-123"
        assert "tok-123" not in str(request.url)
        assert request.url.host == "api.test"


def test_sample_calls_map_to_the_api():
    for name, args, method, path, params in SAMPLES:
        fake = Fake()
        call(server(fake), name, args)
        request = fake.requests[-1]
        assert (request.method, request.url.path) == (method, path), name
        assert dict(request.url.params) == params, name


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "400: invalid"),
        (401, "401: unauthorized"),
        (403, "403: forbidden"),
        (404, "404: not_found"),
        (409, "409: conflict"),
        (422, "422: invalid"),
        (429, "429: rate_limited"),
        (500, "500: upstream_error"),
        (503, "503: upstream_error"),
        (418, "418: error"),
    ],
)
def test_errors_are_a_fixed_message_from_the_status_only(status, message):
    secret = {
        "detail": {
            "error": "forbidden",
            "reason": "leak-reason",
            "secret": "tok-123 do-not-leak",
        },
        "error": {"code": "leak-code", "message": "do-not-leak"},
    }
    name = min(READS)
    with pytest.raises(ToolError) as caught:
        call(server(Fake(status=status, body=secret)), name, ARGS.get(name, {}))
    assert str(caught.value).endswith(": " + message)
    assert "leak" not in str(caught.value)
    assert "tok-123" not in str(caught.value)


def test_token_with_whitespace_or_control_characters_is_refused_without_echo():
    name = min(READS)
    for bad in (
        "two words-abcdefgh",
        "line\nbreak-abcdefgh",
        "tab\there-abcdefgh",
        "nul\x00byte-abcdefgh",
        "   ",
    ):
        fake = Fake()
        srv = build_server(fake.client(), lambda bad=bad: bad).server
        with pytest.raises(ToolError) as caught:
            call(srv, name, ARGS.get(name, {}))
        assert str(caught.value).endswith("401: unauthorized")
        assert "abcdefgh" not in str(caught.value)
        assert fake.requests == []


def test_no_tool_exposes_a_forbidden_argument_name():
    forbidden = {
        "actor",
        "actor_id",
        "workspace",
        "workspace_id",
        "tenant",
        "tenant_id",
        "delegation",
        "token",
        "authorization",
        "owner_id",
    }
    assert forbidden == set(mod.FORBIDDEN_ARGUMENTS)
    for tool in asyncio.run(server(Fake()).list_tools()):
        assert not forbidden & set(tool.input_schema["properties"]), tool.name

    class Bad(mod.Strict):
        actor: str = "x"

    register = mod.Toolbox("x", "x", Fake().client(), lambda: "t").tool(
        "x.bad", Bad, "bad"
    )
    with pytest.raises(ValueError, match="identity arguments are not allowed"):
        register(lambda a: a)


def test_transport_failure_does_not_echo_the_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Authorization: Bearer tok-123 refused")

    srv = build_server(
        httpx.Client(transport=httpx.MockTransport(boom), base_url="https://api.test"),
        lambda: "tok-123",
    ).server
    with pytest.raises(ToolError) as caught:
        call(srv, min(READS), ARGS.get(min(READS), {}))
    assert "tok-123" not in str(caught.value)
    assert "503" in str(caught.value)


def test_url_must_be_https_or_loopback(tmp_path):
    assert mod.api_url(API_KEY, {API_KEY: "https://x.example"}) == "https://x.example"
    assert mod.api_url(API_KEY, {API_KEY: "http://127.0.0.1:9"}) == "http://127.0.0.1:9"
    for bad in (
        "http://example.com",
        "https://u:p@x.example",
        "https://x.example?a=1",
        "https://x.example#f",
    ):
        with pytest.raises(mod.ConfigError):
            mod.api_url(API_KEY, {API_KEY: bad})
    with pytest.raises(mod.ConfigError):
        mod.api_url(API_KEY, {"EMPIRE_CLIENT_CONFIG": str(tmp_path / "none")})


def test_token_order_and_file_mode(tmp_path):
    token_file = tmp_path / "t.jwt"
    token_file.write_text("from-file\n")
    token_file.chmod(0o644)
    with pytest.raises(mod.ConfigError):
        mod.bearer({"EMPIRE_TOKEN_FILE": str(token_file)})
    token_file.chmod(0o600)
    assert mod.bearer({"EMPIRE_TOKEN_FILE": str(token_file)}) == "from-file"
    assert (
        mod.bearer({"EMPIRE_TOKEN": "env", "EMPIRE_TOKEN_FILE": str(token_file)})
        == "env"
    )
    assert (
        mod.bearer(
            {"EMPIRE_MCP_AUTOMATION": "x-y", "EMPIRE_AUTOMATION_TOKEN_X_Y": "auto"}
        )
        == "auto"
    )


def test_automation_name_is_validated_and_tokens_are_cleaned(tmp_path):
    for bad in ("../x", "Owner", "a/b", "-x", ""):
        if bad:
            with pytest.raises(mod.ConfigError):
                mod.bearer({"EMPIRE_MCP_AUTOMATION": bad})
    with pytest.raises(mod.ConfigError) as caught:
        mod.bearer({"EMPIRE_TOKEN": "has space-secret"})
    assert "secret" not in str(caught.value)
    token_file = tmp_path / "t.jwt"
    token_file.write_text("two words\n")
    token_file.chmod(0o600)
    with pytest.raises(mod.ConfigError):
        mod.bearer({"EMPIRE_TOKEN_FILE": str(token_file)})


def _chain(exc: BaseException | None):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        yield exc
        exc = exc.__cause__ or exc.__context__


def test_validation_errors_are_fixed_and_name_no_argument_or_value():
    srv = server(Fake())
    rejected = {}
    for name in sorted(READS | WRITES):
        tool = next(t for t in asyncio.run(srv.list_tools()) if t.name == name)
        attempts = [
            {"UNTRUSTED_MARKER": "VALUE_MARKER"},
            {**ARGS.get(name, {}), "UNTRUSTED_MARKER": 1},
        ]
        attempts += [
            {**ARGS.get(name, {}), field: {"k": "VALUE_MARKER"}}
            for field in tool.input_schema["properties"]
        ]
        for args in attempts:
            try:
                call(srv, name, args)
            except Exception as exc:  # noqa: BLE001
                failure = exc
            else:
                continue
            rejected[name] = rejected.get(name, 0) + 1
            assert str(failure).endswith("invalid_arguments")
            assert "MARKER" not in str(failure)
            assert failure.__cause__ is None
            assert failure.__context__ is None
            for link in _chain(failure):
                assert "MARKER" not in str(link)
                assert "MARKER" not in repr(link)
    assert set(rejected) == READS | WRITES
    with pytest.raises(ToolError) as caught:
        call(srv, "x.UNTRUSTED_MARKER", {"a": "VALUE_MARKER"})
    assert "MARKER" not in str(caught.value) + repr(caught.value)
