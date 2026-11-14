# Ad-hoc views from v1 responses

Use this procedure when an agent or a person builds a one-time view (a table, a chart,
a short report) from tracker data. It is the WP5 part of
[the headless capability scope](../design/headless_capability_layer_scope.md). A view
that you keep moves into Earnings Summary (single-dashboard rule).

## 1. Get the data

- Read only from `/api/v1`, through the [agent adapter](agent-adapter.md) or the HTTP API.
- Save each response body to a local file, unchanged. You can wrap it as
  `{"request": "/api/v1/...", "body": {...}}` to keep the request with it.
- Keep the files and the page on the local machine. Do not publish a page with
  holdings or balances to a hosted service (for example a claude.ai artifact).

## 2. Render

The renderer may format, sort, filter, and chart. It must not compute new financial
values.

- Every number on the page must equal a returned value at the displayed precision.
  A derived value, such as a sum or a difference, is not allowed unless the API
  returns that value.
- Allowed display changes: rounding, thousands separators, a currency sign, a percent
  display of a fraction (`0.123` as `12.3%`), basis points, and `K`/`M`/`B` suffixes.
  Keep the sign of the value.
- A count may be the length of a returned list.
- Show `meta.as_of`, the source providers, and account coverage next to the numbers.
  Show every `meta.warnings` code. Show `NO_DATA` when `meta.as_of` is null.
- When a result has `calculation_status=unavailable`, show its reason codes. Never show
  a blank or a zero instead.
- Mark label numbers that are not data (for example "Top 10" or "6M") with the attribute
  `data-trace-ignore`. The checker lists them so a reviewer can see them.
- Put chart data in `<script type="application/json">` blocks. The checker reads those
  blocks. It does not read other scripts.

## 3. Check

Run the trace checker before you show the page:

```bash
python -m portfolio_tracker.agent.trace_check page.html saved/*.json
```

It fails (exit code 1) when a number or date on the page is not in a saved response,
or when required context (as-of date, providers, warning codes, unavailable reason
codes) is missing. Fix the page; do not change the saved responses.

## Limits

The checker matches values, not meaning. A number that equals a different returned
value at low precision passes. Use enough precision for the trace to be specific, and
label each number with the field it shows.
