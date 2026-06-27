# `GET /api/v1/portfolio/positions` — consolidated positions (v1)

A first-class, server-derived view of the portfolio's current positions. It
moves two derivations the companion `earnings-summary` client used to compute
locally — by joining `GET /api/portfolio/holdings` with `GET /api/plaid/items`
— onto the server, so the tracker owns the contract:

- each position's **`percent_of_portfolio`** (market value ÷ total book), and
- a per-account-lot **`tax_treatment`** (`taxable` / `tax_deferred` /
  `tax_free` / `unknown`) inferred from the account `type` + `subtype`.

This is the additive `/api/v1` namespace: it does not change or replace the
existing `/api/portfolio/holdings` and `/api/plaid/items` endpoints, which keep
working. Once the client consumes this, it can drop its local joins.

## Request

```
GET /api/v1/portfolio/positions
```

No query parameters. The endpoint reports the **latest holdings snapshot** for
the active accounts (the same snapshot the Holdings view consolidates).

## Response `200 OK`

```jsonc
{
  "snapshot_date": "2025-06-02",        // null when there are no active holdings
  "total_market_value": "20000.000000", // sum of position market values (the "book")
  "positions": [
    {
      "security_id": 1,
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "quantity": "100.0000000000",
      "market_value": "10000.000000",
      "cost_basis": "8000.000000",
      "unrealized_pnl": "2000.000000",
      "percent_of_portfolio": "50.0000", // market_value / total_market_value × 100 (percent)
      "accounts": [
        {
          "account_id": 10,
          "account_name": "Taxable Brokerage",
          "quantity": "60.0000000000",
          "market_value": "6000.000000",
          "cost_basis": "4800.000000",
          "cost_basis_source": null,     // null | "manual" | "inferred_acats" | "inferred_1099"
          "tax_treatment": "taxable"
        },
        {
          "account_id": 11,
          "account_name": "Roth IRA",
          "quantity": "40.0000000000",
          "market_value": "4000.000000",
          "cost_basis": "3200.000000",
          "cost_basis_source": null,
          "tax_treatment": "tax_free"
        }
      ]
    }
  ],
  "by_tax_treatment": {                  // market value summed per bucket, at the LOT level
    "taxable": "6000.000000",
    "tax_deferred": "5000.000000",
    "tax_free": "4000.000000",
    "unknown": "5000.000000"
  },
  "notes": ["…"]
}
```

All money fields are JSON strings (SQLAlchemy `Numeric` / `Decimal`) — parse as
decimals, not floats. Whole-dollar rendering is a frontend concern; the API
keeps full precision.

### `percent_of_portfolio`

`market_value / total_market_value × 100`, in **percent** (0–100), matching the
codebase's `weight_pct` convention. A position with no market value is omitted
from the book total and reports `null`. When the book total is 0 (no priced
positions), every position reports `null`.

### `tax_treatment` (4-way)

Inferred from each account's `type` + `subtype`. The mapping is the **finer
4-way** contract — *not* the coarser 3-way
`services/positioning.py:classify_tax_treatment`, which collapses HSA/Roth into
a single "tax-advantaged" slice and maps a bare `individual` to taxable:

| Bucket         | Matches                                                                                          |
| -------------- | ------------------------------------------------------------------------------------------------ |
| `tax_free`     | subtype contains `roth` (incl. Roth 401k/IRA), or subtype is `hsa`                                |
| `tax_deferred` | subtype contains `401k` or `ira`, or is one of `403b`/`457b`/`sep`/`simple`/`pension`/`keogh`/`retirement`/`rrsp`/`sarsep`/`profit sharing plan` |
| `taxable`      | subtype contains `brokerage`, or account `type` is `brokerage`                                    |
| `unknown`      | everything else — including a bare `individual` / `joint` subtype with no `brokerage` token       |

The `roth` check runs first, so "roth ira" / "roth 401k" land in `tax_free`,
not `tax_deferred`. Bucketing is done at the **lot** level, so a position held
in both a Roth IRA and a taxable brokerage contributes to both `tax_free` and
`taxable` in `by_tax_treatment`.

This mapping mirrors the earnings-summary client's `tax_treatment()` exactly,
so switching the client to this endpoint yields the same buckets it derived by
hand.

## Empty book

When there are no active holdings, the endpoint returns `200` with
`snapshot_date: null`, `total_market_value: "0"`, `positions: []`, every
`by_tax_treatment` bucket `"0"`, and a single explanatory note.

## Intentionally omitted (single-user, localhost)

This tracker is a **single-user tool that runs entirely on localhost**. The
following REST conveniences are intentionally **not** implemented; they add
complexity with no benefit here:

- **ETag / conditional GET (`If-None-Match`).** There's one client and the
  payload is small (the current book, not history); a 304-revalidation cache
  saves nothing meaningful. Clients should just re-fetch.
- **Pagination.** A single person's holdings are tens, not thousands, of
  positions. The full set is returned in one response; there is no `limit` /
  `cursor` / `next` contract.

If this ever grows into a multi-user or hosted service, both belong on the
roadmap (and on the additive `/api/v1` surface, which is versioned precisely so
they can be added without breaking existing consumers).
