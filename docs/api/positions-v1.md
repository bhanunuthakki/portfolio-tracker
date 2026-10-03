# `GET /api/v1/portfolio/positions` — consolidated positions (v1)

A first-class, server-derived view of the portfolio's current positions. It
moves two derivations the companion `earnings-summary` client used to compute
locally — by joining `GET /api/portfolio/holdings` with `GET /api/plaid/items`
— onto the server, so the tracker owns the contract:

- each position's **`percent_of_portfolio`** (market value ÷ total book), and
- a per-account-lot **`tax_treatment`** in the detailed five-way enum
  (`taxable` / `pretax` / `roth` / `hsa` / `unknown`) inferred from the
  account `type` + `subtype` (Phase 0 ruling SC-1,
  `docs/design/phase0_decision_addendum.md`).

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
          "tax_treatment": "roth"
        }
      ]
    }
  ],
  "by_tax_treatment": {                  // market value summed per bucket, at the LOT level
    "taxable": "6000.000000",
    "pretax": "5000.000000",
    "roth": "4000.000000",
    "hsa": "0",
    "unknown": "5000.000000"
  },
  "notes": ["…"]
}
```

All money fields are JSON strings (SQLAlchemy `Numeric` / `Decimal`) — parse as
decimals, not floats. Whole-dollar rendering is a frontend concern; the API
keeps full precision.

### `percent_of_portfolio`

`market_value / total_market_value × 100`, in signed **percent**, matching the
codebase's `weight_pct` convention. A position with no market value is omitted
from the book total and reports `null`. When the book total is 0 (no priced
positions), every position reports `null`.

### `tax_treatment` (detailed five-way, SC-1)

Inferred from each account's `type` + `subtype`. The mapping is the ratified
**detailed** contract — *not* the coarser
`services/positioning.py:classify_tax_treatment`, which collapses everything
tax-advantaged into a single display slice and maps a bare `individual` to
taxable:

| Value     | Matches (in precedence order)                                                                    |
| --------- | ------------------------------------------------------------------------------------------------ |
| `roth`    | subtype contains `roth` (incl. Roth 401k/IRA); or account NAME contains `roth`                    |
| `hsa`     | subtype is `hsa`; or name contains `hsa` / `health savings`                                       |
| `pretax`  | subtype contains `401k` or `ira`, or is one of `403b`/`457b`/`sep`/`simple`/`pension`/`keogh`/`retirement`/`rrsp`/`sarsep`/`profit sharing plan`; or name contains `401k`/`401(k)`/word-ish `ira`/`retirement`/`brokeragelink` |
| `taxable` | subtype contains `brokerage`; cash-account subtype (`checking`/`savings`/`cash management`); name contains `brokerage`/`self-directed`/`taxable`; account `type` is `brokerage`; or a bare `individual`/`joint` subtype (LOW confidence) |
| `unknown` | everything else                                                                                   |

Subtype evidence is `high` confidence; cash-subtype/name/type tiers are
`medium`; the bare `individual`/`joint` fallback is `low`. The name tier
exists because SnapTrade omits `subtype` for some institutions (Fidelity
"BrokerageLink", "Health Savings Account") — it centralizes, in the provider,
the heuristics the consumers used to hand-roll. A bare "BrokerageLink" is the
self-directed 401(k) window (owner-confirmed) → `pretax`.

The `roth` check runs first at every tier, so "roth ira" / "BrokerageLink
Roth" land in `roth`, not `pretax`. Bucketing is done at the **lot** level, so
a position held in both a Roth IRA and a taxable brokerage contributes to both
`roth` and `taxable` in `by_tax_treatment`.

Roth and HSA stay distinct because wealthplan models their cash-flow and
withdrawal behavior separately. Consumers needing the old coarse buckets map
`pretax→tax_deferred` and `roth`+`hsa`→`tax_free`. Account-level treatment
with evidence and confidence ships on `GET /api/v1/accounts` (see
`v1-overview.md`).

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

## Signed option holdings (additive contract)

`percent_of_portfolio` uses signed market value divided by the net book. A
written option is a negative liability. An asset weight can exceed 100 percent.
Do not convert these values to absolute values or remove option rows.

Each position now includes nullable `option_contract`, `contract_quantity`, and
`quantity_unit`. Every account lot includes `quantity_unit` and nullable
`contract_quantity`. Securities-master rows include the same `option_contract`.
Portfolio snapshots reuse these exact position models.

`option_contract` contains `underlying_ticker`, `contract_type` (`call` or `put`),
`expiration_date`, `strike_price`, nullable `multiplier`, `metadata_source`, and
nullable `multiplier_source`. Money and quantities remain Decimal strings.
`metadata_source` is `plaid.option_contract`, `snaptrade.option_symbol`, or
`occ_symbol`. Only complete OCC symbols are decoded; adjusted roots remain
unresolved. OCC symbols do not establish a multiplier.

`quantity_unit` is `shares`, `underlying_units`, or `unknown`. New Plaid option
holdings retain their provider-reported underlying units. New SnapTrade option
holdings normalize signed contracts to underlying units using its explicit
`is_mini_option` field (10 or 100). SnapTrade prices are per underlying unit;
its average purchase price is per contract. Each monetary field is multiplied
on its correct basis once. Missing multiplier evidence fails ingestion closed.

Migration `0032` retains existing quantities and values and marks their units
unknown. Existing option holdings cannot establish coverage until an authorized
provider refresh supplies unit evidence. New descriptors persist with the
security; units persist with each holding snapshot. Contract quantities remain
null when unit or multiplier evidence is missing. Non-option holdings retain
their existing share meaning. The adapters provide no holding delta, so this
contract does not invent one. See [covered-call reporting](../covered_call_reporting.md).
