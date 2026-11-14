# Agent adapter (read-only MCP server)

`portfolio_tracker.agent.mcp_server` exposes every `/api/v1` GET resource as one MCP
tool over stdio. It is the WP3 part of
[the headless capability scope](../design/headless_capability_layer_scope.md).

## What it does

- **Tools:** one per v1 GET in [`openapi.v1.json`](openapi.v1.json). The name is the path
  after `/api/v1/`, with `/` and `-` changed to `_` (for example
  `analytics_performance`, `portfolio_positions`, `settings_benchmark_policy`). The
  input schema is the operation's query and path parameters. Unknown arguments are
  refused.
- **Output:** the HTTP response body, unchanged. The `meta` envelope is always part of
  it, so an agent cannot lose `as_of`, coverage, stale or partial flags, or warnings.
  `_meta.request` and `_meta.status` give the request path and HTTP status. A status of
  400 or higher sets `isError`.
- **Resources:** `v1-overview.md`, `positions-v1.md`, `DEFINITIONS.md`, the performance
  methodology note, and [`adhoc-views.md`](adhoc-views.md).
- **No write tools.** Corrections, syncs, backfills, and policy changes stay human
  actions in the console or CLI. The operations contract (WP2) will add preview-only
  tools after its run-log migration is approved.

## Boundaries

- The server calls the tracker API at `PORTFOLIO_TRACKER_API_URL` (default
  `http://127.0.0.1:8000`). It refuses to start with any URL that is not http on a
  loopback host.
- It opens no listener. Its only transport is the stdin and stdout of the process.
- For live data, it runs on the canonical Windows host. The Mac client starts it there
  through the approved key-only SSH route. Live values stay on that route; do not copy
  the database or point the server at a Mac loopback API.
- `PORTFOLIO_TRACKER_MCP_TIMEOUT` sets the per-request timeout in seconds (default 120).

## Client configuration

Register the server in the MCP client on the Mac. Replace `<ssh-config>`, `<host>`, and
`<runtime-root>` with the values of the approved Windows route:

```json
{
  "mcpServers": {
    "portfolio-tracker": {
      "command": "ssh",
      "args": [
        "-F", "<ssh-config>", "-o", "BatchMode=yes", "-T", "<host>",
        "<runtime-root>\\portfolio-tracker\\.venv\\Scripts\\python.exe -m portfolio_tracker.agent.mcp_server"
      ]
    }
  }
}
```

For local development against a disposable database, run
`python -m portfolio_tracker.agent.mcp_server` next to a dev API.

## Verification

`tests/test_agent_mcp_server.py` proves that each tool's output equals the HTTP response
body byte for byte, that every v1 GET has a tool, that tools are read-only, and that a
non-loopback API URL is refused.
