import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api } from "@/api/client";
import { Card, fmtSignedUSD, pnlClass } from "@/components/ui";
import type { InvestmentTransactionOut, TxClassification } from "@/types";

/**
 * Contribution-source detail for the currently-selected window.
 *
 * Shows the per-tx breakdown that adds up to the dashboard's "Net contributions"
 * stat. The cross-window swing the user kept seeing is almost always a few
 * large ACATS / sweep transactions that look like contributions in one feed
 * and internal moves in another — this card surfaces them so they're easy
 * to spot, click through to the Transactions page, and override.
 *
 * Bottom-line total = sum of effective_classification:
 *   external_in   →  +amount  (counts as contribution)
 *   external_out  →  −amount  (counts as withdrawal)
 *   internal      →   0       (excluded)
 *   None          →   0       (buy/sell/dividend; not a cashflow event)
 *
 * Should match the chart's `net_external_cashflow_in` to the dollar.
 */

const CLASSIFICATION_LABEL: Record<TxClassification, string> = {
  external_in: "Contribution",
  external_out: "Withdrawal",
  internal: "Internal",
};

const CLASSIFICATION_CHIP: Record<TxClassification, string> = {
  external_in: "bg-emerald-100 text-emerald-800",
  external_out: "bg-rose-100 text-rose-800",
  internal: "bg-slate-200 text-slate-700",
};

export function ContributionDetailCard({
  startDate,
  endDate,
}: {
  startDate?: string;
  endDate?: string;
}): JSX.Element {
  // Re-uses the transactions endpoint (which honors overrides) rather than
  // the audit endpoint, so the per-row drill-down matches the chart stat
  // exactly without any double-counting.
  const { data, isLoading, isError } = useQuery({
    queryKey: ["transactions", { startDate, endDate, forContribution: true }],
    queryFn: () =>
      api.transactions({ startDate, endDate, limit: 2000 }),
  });

  const cashflowRows = useMemo(
    () =>
      (data ?? []).filter(
        (t) => t.effective_classification !== null,
      ),
    [data],
  );

  const stats = useMemo(() => {
    let inflow = 0;
    let outflow = 0;
    let internalCount = 0;
    let overridden = 0;
    cashflowRows.forEach((t) => {
      const amt = Math.abs(parseFloat(t.amount));
      if (t.effective_classification === "external_in") inflow += amt;
      else if (t.effective_classification === "external_out") outflow += amt;
      else if (t.effective_classification === "internal") internalCount++;
      if (t.override_classification !== null) overridden++;
    });
    return {
      inflow,
      outflow,
      net: inflow - outflow,
      inflowCount: cashflowRows.filter(
        (t) => t.effective_classification === "external_in",
      ).length,
      outflowCount: cashflowRows.filter(
        (t) => t.effective_classification === "external_out",
      ).length,
      internalCount,
      overridden,
    };
  }, [cashflowRows]);

  const sortedRows = useMemo(
    () =>
      [...cashflowRows].sort(
        (a, b) => Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)),
      ),
    [cashflowRows],
  );

  return (
    <Card>
      <header className="border-b border-slate-100 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-900">
          Contribution detail (window)
        </h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Every cashflow tx that contributes to the &ldquo;Net contributions&rdquo;
          stat above. Click any row&apos;s &ldquo;Counted as&rdquo; chip on the
          Transactions page to override.
        </p>
      </header>

      {isLoading ? (
        <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
      ) : isError ? (
        <div className="px-4 py-6 text-xs text-red-700">
          Failed to load contribution detail.
        </div>
      ) : cashflowRows.length === 0 ? (
        <div className="px-4 py-6 text-xs text-slate-500">
          No cashflow transactions in this window.
        </div>
      ) : (
        <>
          <SummaryStrip stats={stats} />
          <DetailTable rows={sortedRows} />
        </>
      )}
    </Card>
  );
}

function SummaryStrip({
  stats,
}: {
  stats: {
    inflow: number;
    outflow: number;
    net: number;
    inflowCount: number;
    outflowCount: number;
    internalCount: number;
    overridden: number;
  };
}): JSX.Element {
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-b border-slate-100 px-4 py-3 sm:grid-cols-4">
      <Stat
        label="Net (window)"
        value={fmtSignedUSD(stats.net)}
        valueClass={pnlClass(stats.net)}
      />
      <Stat
        label={`Inflows (${stats.inflowCount})`}
        value={`+$${stats.inflow.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
        valueClass="text-emerald-700"
      />
      <Stat
        label={`Outflows (${stats.outflowCount})`}
        value={`-$${stats.outflow.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
        valueClass="text-rose-700"
      />
      <Stat
        label="Excluded / overridden"
        value={
          stats.internalCount > 0 || stats.overridden > 0
            ? `${stats.internalCount} internal · ${stats.overridden} overridden`
            : "0"
        }
      />
    </div>
  );
}

function Stat({
  label,
  value,
  valueClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
}): JSX.Element {
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-wider text-slate-500">
        {label}
      </div>
      <div
        className={[
          "mt-0.5 text-sm font-semibold tabular-nums truncate",
          valueClass ?? "text-slate-900",
        ].join(" ")}
      >
        {value}
      </div>
    </div>
  );
}

function DetailTable({
  rows,
}: {
  rows: InvestmentTransactionOut[];
}): JSX.Element {
  const visible = rows.slice(0, 30);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="bg-slate-50 text-slate-600">
          <tr>
            <th className="px-3 py-2 text-left font-medium">Date</th>
            <th className="px-3 py-2 text-left font-medium">Account</th>
            <th className="px-3 py-2 text-left font-medium">Type</th>
            <th className="px-3 py-2 text-left font-medium">Description</th>
            <th className="px-3 py-2 text-right font-medium">Amount</th>
            <th className="px-3 py-2 text-left font-medium">Counted as</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((t) => {
            const amt = parseFloat(t.amount);
            const eff = t.effective_classification;
            const signedAmt =
              eff === "external_in"
                ? Math.abs(amt)
                : eff === "external_out"
                  ? -Math.abs(amt)
                  : amt;
            return (
              <tr
                key={t.plaid_investment_transaction_id}
                className="border-t border-slate-100"
              >
                <td className="px-3 py-1.5 tabular-nums text-slate-900">
                  {t.date}
                </td>
                <td className="px-3 py-1.5 text-slate-700">{t.account_name}</td>
                <td className="px-3 py-1.5">
                  <span className="inline-flex rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase text-slate-700">
                    {t.subtype ?? t.type}
                  </span>
                </td>
                <td
                  className="px-3 py-1.5 text-slate-600 max-w-[280px]"
                  title={t.name ?? ""}
                >
                  <span className="block truncate">{t.name ?? "—"}</span>
                </td>
                <td
                  className={`px-3 py-1.5 text-right tabular-nums ${pnlClass(signedAmt)}`}
                >
                  {fmtSignedUSD(signedAmt)}
                </td>
                <td className="px-3 py-1.5">
                  {eff !== null ? (
                    <span
                      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${CLASSIFICATION_CHIP[eff]}`}
                    >
                      {CLASSIFICATION_LABEL[eff]}
                      {t.override_classification !== null ? (
                        <span className="text-[8px] uppercase opacity-70">
                          override
                        </span>
                      ) : null}
                    </span>
                  ) : (
                    <span className="text-slate-400">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length > visible.length && (
        <p className="border-t border-slate-100 px-4 py-2 text-xs text-slate-500">
          Showing top {visible.length} of {rows.length} cashflow rows by absolute
          amount. See Transactions page for the full list.
        </p>
      )}
    </div>
  );
}
