import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import { DataQualityReport } from "@/components/DataQualityReport";
import {
  Card,
  EmptyState,
  ErrorBanner,
  fmtSignedUSD,
  fmtUSD,
  pnlClass,
  Td,
  Th,
} from "@/components/ui";
import type { ConsolidatedHoldingOut } from "@/types";

export function Holdings(): JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["holdings"],
    queryFn: () => api.latestHoldings(),
  });

  if (isLoading) return <div className="text-sm text-slate-500">Loading…</div>;
  if (isError)
    return <ErrorBanner>Failed to load holdings.</ErrorBanner>;
  if (!data || data.length === 0) {
    return (
      <EmptyState>
        No holdings yet. Link an account, then run the snapshotter.
      </EmptyState>
    );
  }

  return (
    <div className="space-y-4">
      <DataQualityReport />
      <Card className="overflow-hidden">
        <table className="min-w-full divide-y divide-slate-200">
          <thead className="bg-slate-50">
            <tr>
              <Th>Ticker</Th>
              <Th>Name</Th>
              <Th align="right">Quantity</Th>
              <Th align="right">Avg cost / share</Th>
              <Th align="right">Cost basis</Th>
              <Th align="right">Value</Th>
              <Th align="right">Unrealized</Th>
              <Th align="right">Accounts</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.map((h) => (
              <Row key={h.security_id} h={h} />
            ))}
          </tbody>
        </table>
        <p className="border-t border-slate-100 bg-slate-50 px-4 py-2.5 text-xs text-slate-500">
          Positions consolidated by ticker. Click any row to expand the
          per-account drill-down. Avg cost = total cost basis ÷ total shares.
        </p>
      </Card>
    </div>
  );
}

function Row({ h }: { h: ConsolidatedHoldingOut }): JSX.Element {
  const [open, setOpen] = useState(false);
  const value = h.total_value !== null ? parseFloat(h.total_value) : null;
  const cost = h.total_cost_basis !== null ? parseFloat(h.total_cost_basis) : null;
  const unrealized = h.unrealized_pnl !== null ? parseFloat(h.unrealized_pnl) : null;
  const avgCost =
    h.weighted_avg_cost_per_share !== null
      ? parseFloat(h.weighted_avg_cost_per_share)
      : null;
  const accountCount = h.accounts.length;

  return (
    <>
      <tr
        className="cursor-pointer hover:bg-slate-50"
        onClick={() => setOpen(!open)}
      >
        <Td className="font-mono text-slate-900">
          <span className="inline-block w-3 text-slate-400">
            {open ? "▾" : "▸"}
          </span>{" "}
          {h.ticker ?? "—"}
        </Td>
        <Td className="text-slate-600">{h.name ?? "—"}</Td>
        <Td align="right" className="tabular-nums">
          {parseFloat(h.total_quantity).toLocaleString(undefined, {
            maximumFractionDigits: 4,
          })}
        </Td>
        <Td align="right" className="tabular-nums">
          {avgCost !== null ? `$${avgCost.toFixed(2)}` : "—"}
        </Td>
        <Td align="right" className="tabular-nums">
          {fmtUSD(cost)}
        </Td>
        <Td
          align="right"
          className="tabular-nums font-medium text-slate-900"
        >
          {fmtUSD(value)}
        </Td>
        <Td
          align="right"
          className={["tabular-nums", pnlClass(unrealized)].join(" ")}
        >
          {fmtSignedUSD(unrealized)}
        </Td>
        <Td align="right" className="text-slate-500">
          {accountCount} {accountCount === 1 ? "account" : "accounts"}
        </Td>
      </tr>
      {open && (
        <tr className="bg-slate-50/60">
          <td colSpan={8} className="px-4 py-3">
            <table className="min-w-full text-xs">
              <thead className="text-slate-500 uppercase tracking-wide">
                <tr>
                  <th className="pb-1 text-left font-medium">Account</th>
                  <th className="pb-1 text-right font-medium">Quantity</th>
                  <th className="pb-1 text-right font-medium">Cost basis</th>
                  <th className="pb-1 text-right font-medium">Value</th>
                  <th className="pb-1 text-right font-medium">% of position</th>
                </tr>
              </thead>
              <tbody>
                {h.accounts.map((a) => {
                  const aValue =
                    a.institution_value !== null
                      ? parseFloat(a.institution_value)
                      : null;
                  const aCost =
                    a.cost_basis !== null ? parseFloat(a.cost_basis) : null;
                  const pct =
                    aValue !== null && value !== null && value > 0
                      ? (aValue / value) * 100
                      : null;
                  return (
                    <tr key={a.account_id}>
                      <td className="py-1 text-slate-700">{a.account_name}</td>
                      <td className="py-1 text-right tabular-nums">
                        {parseFloat(a.quantity).toLocaleString(undefined, {
                          maximumFractionDigits: 4,
                        })}
                      </td>
                      <td className="py-1 text-right tabular-nums">
                        {fmtUSD(aCost)}
                      </td>
                      <td className="py-1 text-right tabular-nums">
                        {fmtUSD(aValue)}
                      </td>
                      <td className="py-1 text-right tabular-nums text-slate-500">
                        {pct !== null ? `${pct.toFixed(0)}%` : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  );
}
