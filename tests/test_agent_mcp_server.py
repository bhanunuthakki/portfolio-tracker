"""The read-only MCP adapter mirrors /api/v1 GETs byte for byte and cannot write."""

from __future__ import annotations

import io
import json
from datetime import date
from typing import Any

import httpx
import pytest

from portfolio_tracker.agent import mcp_server
from portfolio_tracker.agent.mcp_server import (
    OPENAPI_PATH,
    RESOURCES,
    ConfigurationError,
    TrackerMcpServer,
    require_loopback,
    serve,
)

SPEC: dict[str, Any] = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _rpc(server: TrackerMcpServer, method: str, params: dict[str, Any] | None = None) -> Any:
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert reply is not None
    return reply


def _offline_server() -> TrackerMcpServer:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(refuse))
    return TrackerMcpServer(http, SPEC)


def test_every_v1_get_is_a_read_only_tool() -> None:
    server = _offline_server()
    tools = _rpc(server, "tools/list")["result"]["tools"]
    v1_paths = SPEC["paths"].items()
    reads = {p for p, ops in v1_paths if p.startswith("/api/v1/") and "get" in ops}
    previews = {p for p, ops in v1_paths if p.endswith("/preview") and "post" in ops}

    assert len(tools) == len(reads) + len(previews)
    assert {t["description"].split(" ")[1].rstrip(".") for t in tools} == reads | previews
    assert all(
        t["description"].startswith("GET ") or t["name"] == "operations_preview" for t in tools
    )
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["inputSchema"]["additionalProperties"] is False


def test_tool_output_is_the_http_body_byte_for_byte(client: Any, session: Any) -> None:
    from portfolio_tracker.api.fixtures_v1 import _seed  # pyright: ignore[reportPrivateUsage]

    _seed(session, holdings_date=date.today())
    session.commit()
    recorded: list[httpx.Response] = []

    def record(response: httpx.Response) -> None:
        response.read()
        recorded.append(response)

    client.event_hooks = {"request": [], "response": [record]}
    server = TrackerMcpServer(client, SPEC)
    checked = 0
    for tool in _rpc(server, "tools/list")["result"]["tools"]:
        if tool["inputSchema"]["required"]:
            continue
        recorded.clear()
        result = _rpc(server, "tools/call", {"name": tool["name"], "arguments": {}})["result"]

        assert len(recorded) == 1, tool["name"]
        assert result["content"] == [{"type": "text", "text": recorded[0].text}], tool["name"]
        assert result["isError"] is (recorded[0].status_code >= 400)
        assert result["_meta"]["status"] == recorded[0].status_code
        checked += 1
    assert checked >= 20


def test_arguments_become_the_same_query(client: Any) -> None:
    server = TrackerMcpServer(client, SPEC)
    result = _rpc(
        server,
        "tools/call",
        {
            "name": "analytics_benchmark_estimate",
            "arguments": {"benchmarks": ["VOO", "QQQ"], "lookback_days": [180], "end_date": None},
        },
    )["result"]

    assert result["_meta"]["request"] == (
        "/api/v1/analytics/benchmark-estimate?benchmarks=VOO&benchmarks=QQQ&lookback_days=180"
    )
    path_result = _rpc(
        server,
        "tools/call",
        {"name": "valuation_observations", "arguments": {"observation_key": "a/b"}},
    )["result"]
    assert path_result["_meta"]["request"] == "/api/v1/valuation-observations/a%2Fb"


def test_unknown_arguments_and_tools_are_refused() -> None:
    server = _offline_server()

    unknown_arg = _rpc(server, "tools/call", {"name": "health", "arguments": {"sql": "x"}})
    assert unknown_arg["result"]["isError"] is True
    unknown_tool = _rpc(server, "tools/call", {"name": "delete_everything", "arguments": {}})
    assert unknown_tool["error"]["code"] == -32602
    offline = _rpc(server, "tools/call", {"name": "health", "arguments": {}})["result"]
    assert offline["isError"] is True
    assert offline["content"][0]["text"] == "tracker API unreachable: ConnectError"


def test_protocol_handshake_and_errors() -> None:
    server = _offline_server()

    init = _rpc(server, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert "tools" in init["capabilities"]
    assert "meta" in init["instructions"]
    assert (
        _rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"]
        == mcp_server.SUPPORTED_PROTOCOLS[0]
    )
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _rpc(server, "ping")["result"] == {}
    assert _rpc(server, "tools/write")["error"]["code"] == -32601
    assert server.handle({"id": 1})["error"]["code"] == -32600


def test_resources_are_readable_reference_docs() -> None:
    server = _offline_server()

    listed = _rpc(server, "resources/list")["result"]["resources"]
    assert [r["uri"] for r in listed] == [uri for uri, _rel, _desc in RESOURCES]
    for resource in listed:
        contents = _rpc(server, "resources/read", {"uri": resource["uri"]})["result"]["contents"]
        assert contents[0]["text"].strip()
    assert _rpc(server, "resources/read", {"uri": "file:///etc/passwd"})["error"]["code"] == -32002


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8000",
        "https://127.0.0.1:8000",
        "http://tracker.example:8000",
        "http://0.0.0.0:8000",
    ],
)
def test_non_loopback_api_url_is_refused(url: str) -> None:
    with pytest.raises(ConfigurationError):
        require_loopback(url)


def test_loopback_api_url_is_accepted() -> None:
    assert require_loopback("http://localhost:8000/") == "http://localhost:8000"
    assert require_loopback("http://[::1]:8000") == "http://[::1]:8000"


def test_stdio_framing_is_lf_delimited_json() -> None:
    server = _offline_server()
    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\r\n'
        b"not json\n"
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    )
    stdout = io.BytesIO()

    serve(server, stdin, stdout)

    lines = stdout.getvalue().split(b"\n")
    assert lines[-1] == b""
    assert json.loads(lines[0]) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert json.loads(lines[1])["error"]["code"] == -32700
    assert len(lines) == 3
