# Covered-call reporting

A short option has a negative market value. That value is a liability and belongs
in portfolio net value. Do not remove it or convert it to a positive asset.

The dashboard groups a call with its underlying stock. It shows stock value,
option value, and their net value separately. Stock capital concentration is not
the same as sensitivity to a stock-price change. The current provider adapters do
not supply a reliable option delta, so this release does not estimate that
sensitivity.

Coverage uses stock and short calls in the same account. It requires a known
underlying and a quantity expressed in underlying units. A known multiplier is
also required to display the contract count. Shares cannot cover multiple
calls at once. Unknown or unsupported contract metadata remains visible as
incomplete evidence. An OCC symbol can identify a call, strike, and expiry, but
does not establish its deliverable multiplier.

## Provider units

Plaid reports option quantity in underlying units. SnapTrade reports option
quantity in contracts and option price per underlying share. The adapter
normalizes new SnapTrade option holdings to underlying units once, using its
documented standard or mini multiplier. Contract quantity is a separate derived
field. Existing holdings retain unknown units until a provider refresh supplies
the evidence; migration does not guess or change their financial values.

Provider references: [Plaid Investments API](https://plaid.com/docs/api/products/investments/)
and [SnapTrade account holdings](https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getUserHoldings).
The existing SnapTrade holdings endpoint is deprecated for new customers. This
change repairs the current adapter; it does not migrate to a new endpoint.

## Confirmed short calls reported as long

If a SoFi feed reports a confirmed short call with positive quantity or
market value, first verify the position and confirm that the affected holding
is present in the tracker's Plaid connection. Keep each confirmed exception
in the local, gitignored `data/plaid_short_calls.json` file. The file is a JSON
array of records with a `plaid_account_id`, full `occ_symbol`, `confirmed_on`
date (YYYY-MM-DD), and `evidence` explaining how the short position was verified.
Register verified contracts with `python -m portfolio_tracker.jobs.register_plaid_short_calls`
using `--account-mask`, a `--symbol` for each contract, `--confirmed-on`,
and `--evidence`. The command checks the latest broker snapshot of the
matching active Plaid account and preserves existing rules.

Every Plaid snapshot applies matching rules from the confirmation date
through contract expiry. The snapshot stores negative quantity, value, and
cost basis where available and keeps the per-unit price and broker account
total as reported. Rerun the Plaid snapshot to correct today's rows and
refresh today's cached value. Preserve the local file when updating the
checkout. Remove a rule when its confirmed short position closes, especially
before opening a long position in the same contract. Earlier snapshots are
outside the scope of a fresh import.

## Performance

Writing a call increases cash and creates an option liability. The premium alone
is not immediate investment profit. Portfolio performance uses complete account
values and external contributions or withdrawals. Option purchases, sales,
closing trades, expiration, and assignment are internal investment activity.

Complete broker account-value observations avoid reconstructing each historical
option price. If a historical interval lacks those observations or required flow
evidence, the existing completeness gate still applies. Start with Plaid and
SnapTrade. Request a broker export only for an identified account, date range,
and missing field. Missing archive assurance can reduce certification without
necessarily preventing a return calculation.

This release does not import historical data, invent option prices, or change
live financial records. Production migration and provider refresh remain
separate operations.
