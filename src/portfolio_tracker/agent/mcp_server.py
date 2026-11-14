"""Read-only MCP server over the tracker's `/api/v1` GET resources (stdio transport).

Each v1 GET in the checked-in OpenAPI document becomes one tool. A tool call sends
the same GET to the tracker API on loopback and returns the response body unchanged,
so the `meta` envelope (as-of, coverage, stale/partial flags, warnings, methodology)
always reaches the agent. There are no write tools: commits stay human actions.

Live use runs this module on the canonical Windows host, started over the approved
key-only SSH route (see docs/api/agent-adapter.md). It never opens a listener.

    python -m portfolio_tracker.agent.mcp_server
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import quote, urlsplit

import httpx

SERVER_NAME = "portfolio-tracker"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 120.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = REPO_ROOT / "docs" / "api" / "openapi.v1.json"

# (uri, repo-relative path, description). Read-only reference material for agents.
RESOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "tracker://docs/v1-overview",
        "docs/api/v1-overview.md",
        "Envelope, freshness, numeric conventions, and errors for every v1 resource.",
    ),
    (
        "tracker://docs/positions-v1",
        "docs/api/positions-v1.md",
        "Account-level positions resource.",
    ),
    (
        "tracker://docs/definitions",
        "DEFINITIONS.md",
        "Authoritative domain vocabulary (Positioning, Asset type, Sector, Region, ...).",
    ),
    (
        "tracker://docs/performance-methodology",
        "docs/design/provenance_backed_performance_reconstruction.md",
        "How canonical performance is certified and why it fails closed.",
    ),
    (
        "tracker://docs/adhoc-views",
        "docs/api/adhoc-views.md",
        "Rules and trace checker for rendering an ad-hoc view from v1 responses.",
    ),
)

INSTRUCTIONS = (
    "Read-only portfolio facts from the tracker's /api/v1 contract. Every tool returns "
    "the API response body unchanged, including its `meta` block. Show `meta.as_of`, "
    "source providers, coverage, and every warning next to the numbers they affect. "
    "When a result has calculation_status=unavailable, show its reason codes; never "
    "show a blank or zero instead. Do not compute new financial values from these "
    "responses; a displayed value must equal a returned value. There are no write "
    "tools: corrections, syncs, and backfills stay human actions. To render a view, "
    "follow the resource tracker://docs/adhoc-views."
)

_JSON = dict[str, Any]


class ConfigurationError(RuntimeError):
    """The server cannot start with the given configuration."""


def require_loopback(api_url: str) -> str:
    """Return `api_url` without a trailing slash, or refuse a non-loopback target."""
    parts = urlsplit(api_url)
    if parts.scheme != "http" or (parts.hostname or "") not in LOOPBACK_HOSTS:
        raise ConfigurationError(
            "PORTFOLIO_TRACKER_API_URL must be an http loopback URL on the host that "
            "owns the tracker API (for example http://127.0.0.1:8000)."
        )
    return api_url.rstrip("/")


def tool_name_for(path: str) -> str:
    """`/api/v1/analytics/after-tax` -> `analytics_after_tax`."""
    segments = [s for s in path.removeprefix("/api/v1/").split("/") if not s.startswith("{")]
    return "_".join(segments).replace("-", "_")


def load_tools(spec: _JSON) -> dict[str, _JSON]:
    """Build one tool definition per v1 GET operation, keyed by tool name."""
    tools: dict[str, _JSON] = {}
    paths = cast(dict[str, dict[str, _JSON]], spec["paths"])
    for path, operations in sorted(paths.items()):
        operation = operations.get("get")
        if not path.startswith("/api/v1/") or operation is None:
            continue
        properties: _JSON = {}
        required: list[str] = []
        path_params: list[str] = []
        for param in cast(list[_JSON], operation.get("parameters", [])):
            name = cast(str, param["name"])
            schema = dict(cast(_JSON, param.get("schema", {})))
            if "$ref" in json.dumps(schema):
                raise ConfigurationError(f"unsupported $ref in parameter {name} of {path}")
            if param.get("description"):
                schema["description"] = param["description"]
            properties[name] = schema
            if param.get("required"):
                required.append(name)
            if param.get("in") == "path":
                path_params.append(name)
        name = tool_name_for(path)
        if name in tools:
            raise ConfigurationError(f"duplicate tool name {name}")
        description = cast(str, operation.get("description") or operation.get("summary") or "")
        tools[name] = {
            "definition": {
                "name": name,
                "title": cast(str, operation.get("summary") or name),
                "description": f"GET {path}. {description}".strip(),
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            },
            "path": path,
            "path_params": path_params,
            "params": set(properties),
        }
    return tools


def _text_result(text: str, *, is_error: bool, meta: _JSON | None = None) -> _JSON:
    result: _JSON = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if meta is not None:
        result["_meta"] = meta
    return result


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TrackerMcpServer:
    """Protocol handler. `http` must already target the tracker API base URL."""

    def __init__(self, http: httpx.Client, spec: _JSON, root: Path = REPO_ROOT) -> None:
        self._http = http
        self._tools = load_tools(spec)
        self._root = root

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    # -- JSON-RPC dispatch -------------------------------------------------

    def handle(self, message: Any) -> Any:
        """Handle one JSON-RPC message or batch; return the reply or None."""
        if isinstance(message, list):
            replies = [self.handle(item) for item in cast(list[Any], message)]
            replies = [r for r in replies if r is not None]
            return replies or None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "invalid request")
        request = cast(_JSON, message)
        method = request.get("method")
        is_notification = "id" not in request
        request_id = request.get("id")
        if not isinstance(method, str):
            return None if is_notification else _rpc_error(request_id, -32600, "invalid request")
        params: Any = request.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise RpcError(-32602, "params must be an object")
            result = self._dispatch(method, cast(_JSON, params))
        except RpcError as exc:
            return None if is_notification else _rpc_error(request_id, exc.code, exc.message)
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: _JSON) -> _JSON:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "resources": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method.startswith("notifications/"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [self._tools[n]["definition"] for n in self.tool_names]}
        if method == "tools/call":
            return self._call_tool(params)
        if method == "resources/list":
            return {
                "resources": [
                    {"uri": uri, "name": rel, "description": desc, "mimeType": "text/markdown"}
                    for uri, rel, desc in RESOURCES
                ]
            }
        if method == "resources/read":
            return self._read_resource(params)
        raise RpcError(-32601, f"method not found: {method}")

    # -- tools -------------------------------------------------------------

    def _call_tool(self, params: _JSON) -> _JSON:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise RpcError(-32602, f"unknown tool: {name}")
        arguments: Any = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "arguments must be an object")
        args = cast(_JSON, arguments)
        unknown = sorted(set(args) - cast(set[str], tool["params"]))
        if unknown:
            return _text_result(f"unknown arguments: {', '.join(unknown)}", is_error=True)
        path = cast(str, tool["path"])
        query: _JSON = {}
        for key, value in args.items():
            if value is None:
                continue
            if key in cast(list[str], tool["path_params"]):
                path = path.replace("{" + key + "}", quote(str(value), safe=""))
            else:
                query[key] = value
        if "{" in path:
            return _text_result("missing required path argument", is_error=True)
        try:
            response = self._http.get(path, params=query)
        except httpx.HTTPError as exc:
            return _text_result(
                f"tracker API unreachable: {type(exc).__name__}",
                is_error=True,
            )
        request = str(response.request.url.raw_path, "ascii")
        return _text_result(
            response.text,
            is_error=response.status_code >= 400,
            meta={"request": request, "status": response.status_code},
        )

    # -- resources ---------------------------------------------------------

    def _read_resource(self, params: _JSON) -> _JSON:
        uri = params.get("uri")
        for known_uri, rel, _desc in RESOURCES:
            if uri == known_uri:
                text = (self._root / rel).read_text(encoding="utf-8")
                return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}
        raise RpcError(-32002, f"resource not found: {uri}")


def _rpc_error(request_id: Any, code: int, message: str) -> _JSON:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(server: TrackerMcpServer, stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Newline-delimited JSON-RPC over byte streams (binary, so Windows keeps LF)."""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: Any = _rpc_error(None, -32700, "parse error")
        else:
            reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


_TIMEOUT_RE = re.compile(r"^\d+(\.\d+)?$")


def main() -> None:
    try:
        base = require_loopback(os.environ.get("PORTFOLIO_TRACKER_API_URL", DEFAULT_API_URL))
        raw_timeout = os.environ.get("PORTFOLIO_TRACKER_MCP_TIMEOUT", "")
        timeout = float(raw_timeout) if _TIMEOUT_RE.match(raw_timeout) else DEFAULT_TIMEOUT_SECONDS
        spec = cast(_JSON, json.loads(OPENAPI_PATH.read_text(encoding="utf-8")))
    except (ConfigurationError, OSError, ValueError) as exc:
        sys.stderr.write(f"portfolio-tracker MCP server cannot start: {exc}\n")
        raise SystemExit(2) from None
    with httpx.Client(base_url=base, timeout=timeout) as http:
        serve(TrackerMcpServer(http, spec), sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
