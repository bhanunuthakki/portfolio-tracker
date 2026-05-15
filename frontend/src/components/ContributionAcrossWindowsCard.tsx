import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api } from "@/api/client";
import { Card, fmtSignedUSD, pnlClass } from "@/components/ui";
import type { InvestmentTransactionOut } from "@/types";

/**
 * Cross-window contribution comparison.
 *
 * Directly addresses the user's "why is my contribution number swinging
 * wildly across 6M / 1Y / 2Y?" question. Each row is a window; the
 * delta-from-shorter-window column attributes the jump to specific
 * transactions that exist in the wider window but not the narrower one.
 *
 * Fetches one big slice of transactions (5y back, capped at 2000) and
 * computes everything client-side so window switching is instant.
 */

type WindowDef = { label: string; days: number };

const WINDOWS: WindowDef[] = [
  { label: "6M", days: 180 },
  { label: "1Y", days: 365 },
  { label: "2Y", days: 730 },
  { label: "5Y", days: 1825 },
];

export function ContributionAcrossWindowsCard(): JSX.Element {
  // Pull a big slice once. 5y * ~200 cashflow tx/y < 2000 cap.
  const fiveYearsAgo = useMemo(() => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - 1825);
    return d.toISOString().slice(0, 10);
  }, []);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["transactions", { startDate: fiveYearsAgo, limit: 2000 }],
    queryFn: () =>
      api.transactions({ startDate: fiveYearsAgo, limit: 2000 }),
  });

  const computed = useMemo(() => {
    if (!data) return null;
    const today = new Date();
    return WINDOWS.map(({ label, days }) => {
      const cutoff = new Date(today);
      cutoff.setUTCDate(cutoff.getUTCDate() - days);
      const cutoffStr = cutoff.toISOString().slice(0, 10);

      const inflows: InvestmentTransactionOut[] = [];
      const outflows: InvestmentTransactionOut[] = [];
      for (const t of data) {
        if (t.date < cutoffStr) continue;
        if (t.effective_classification === "external_in") inflows.push(t);
        else if (t.effective_classification === "external_out") outflows.push(t);
      }
      const inflowTotal = inflows.reduce(
        (s, t) => s + Math.abs(parseFloat(t.amount)),
        0,
      );
      const outflowTotal = outflows.reduce(
        (s, t) => s + Math.abs(parseFloat(t.amount)),
        0,
      );
      return {
        label,
        days,
        cutoff: cutoffStr,
        net: inflowTotal - outflowTotal,
        inflowCount: inflows.length,
        outflowCount: outflows.length,
        inflowTotal,
        outflowTotal,
      };
    });
  }, [data]);

  // For each consecutive (smaller, larger) pair, compute what tx are in
  // the larger window but not the smaller — these explain the delta.
  const deltaRows = useMemo(() => {
    if (!data || !computed) return null;
    const result: Array<{
      pair: string;
      delta: number;
      explanationTxs: InvestmentTransactionOut[];
    }> = [];
    for (let i = 1; i < computed.length; i++) {
      const smaller = computed[i - 1];
      const larger = computed[i];
      const explanationTxs = data
        .filter(
          (t) =>
            t.date >= larger.cutoff &&
            t.date < smaller.cutoff &&
            (t.effective_classification === "external_in" ||
              t.effective_classification === "external_out"),
        )
        .sort(
          (a, b) =>
            Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)),
        );
      result.push({
        pair: `${larger.label} vs ${smaller.label}`,
        delta: larger.net - smaller.net,
        explanationTxs: explanationTxs.slice(0, 5),
      });
    }
    return result;
  }, [data, computed]);

  return (
    <Card>
      <header className="border-b border-slate-100 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-900">
          Contributions across windows
        </h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Why your contribution stat changes when you flip presets. Each
          delta row lists the biggest transactions that exist in the wider
          window but NOT in the narrower one.
        </p>
      </header>

      {isLoading ? (
        <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
      ) : isError || !computed ? (
        <div className="px-4 py-6 text-xs text-red-700">
          Failed to load.
        </div>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-slate-50 text-slate-600">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Window</th>
                  <th className="px-3 py-2 text-right font-medium">Net</th>
                  <th className="px-3 py-2 text-right font-medium">Inflows</th>
                  <th className="px-3 py-2 text-right font-medium">Outflows</th>
                </tr>
              </thead>
              <tbody>
                {computed.map((w) => (
                  <tr
                    key={w.label}
                    className="border-t border-slate-100"
                  >
                    <td className="px-3 py-1.5 font-medium text-slate-900">
                      {w.label}
                      <span className="ml-1.5 text-[10px] text-slate-500">
                        since {w.cutoff}
                      </span>
                    </td>
                    <td
                      className={`px-3 py-1.5 text-right font-semibold tabular-nums ${pnlClass(w.net)}`}
                    >
                      {fmtSignedUSD(w.net)}
                    </td>
                    <td className="px-3 py-1.5 text-right text-emerald-700 tabular-nums">
                      +${w.inflowTotal.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                      <span className="ml-1 text-[10px] text-slate-500">({w.inflowCount})</span>
                    </td>
                    <td className="px-3 py-1.5 text-right text-rose-700 tabular-nums">
                      -${w.outflowTotal.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                      <span className="ml-1 text-[10px] text-slate-500">({w.outflowCount})</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {deltaRows && deltaRows.length > 0 && (
            <div className="border-t border-slate-100 bg-slate-50 px-4 py-3 space-y-3">
              {deltaRows.map((row) => (
                <DeltaExplanation key={row.pair} row={row} />
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  );
}

function DeltaExplanation({
  row,
}: {
  row: {
    pair: string;
    delta: number;
    explanationTxs: InvestmentTransactionOut[];
  };
}): JSX.Element {
  if (row.explanationTxs.length === 0) {
    return (
      <div className="text-xs text-slate-600">
        <span className="font-medium">{row.pair}</span> · delta{" "}
        <span className={pnlClass(row.delta)}>{fmtSignedUSD(row.delta)}</span> · no cashflow
        transactions in the new window.
      </div>
    );
  }
  return (
    <div>
      <div className="text-xs text-slate-600">
        <span className="font-medium">{row.pair}</span> · delta{" "}
        <span className={`font-semibold ${pnlClass(row.delta)}`}>
          {fmtSignedUSD(row.delta)}
        </span>{" "}
        explained by {row.explanationTxs.length} tx in the wider window only:
      </div>
      <ul className="mt-1 space-y-0.5 text-[11px] text-slate-700">
        {row.explanationTxs.map((t) => {
          const amt = parseFloat(t.amount);
          const signed =
            t.effective_classification === "external_in"
              ? Math.abs(amt)
              : -Math.abs(amt);
          return (
            <li
              key={t.plaid_investment_transaction_id}
              className="flex items-center gap-2"
            >
              <span className="text-slate-500 tabular-nums w-20 shrink-0">
                {t.date}
              </span>
              <span className="text-slate-500 truncate max-w-[180px]" title={t.account_name}>
                {t.account_name}
              </span>
              <span className={`tabular-nums font-medium ${pnlClass(signed)}`}>
                {fmtSignedUSD(signed)}
              </span>
              <span className="text-slate-500 truncate" title={t.name ?? ""}>
                {t.name ?? "—"}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
