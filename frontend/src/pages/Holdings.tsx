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
import type { ConsolidatedHoldingOut, CostBasisSource } from "@/types";

const SOURCE_LABEL: Record<CostBasisSource, string> = {
  manual: "manual",
  inferred_acats: "ACATS inferred",
  inferred_1099: "1099 inferred",
};

const SOURCE_CLASS: Record<CostBasisSource, string> = {
  manual: "bg-amber-100 text-amber-800",
  inferred_acats: "bg-sky-100 text-sky-800",
  inferred_1099: "bg-indigo-100 text-indigo-800",
};

function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const d = new Date(iso + "T00:00:00Z").getTime();
  const today = new Date(new Date().toISOString().slice(0, 10) + "T00:00:00Z").getTime();
  return Math.round((d - today) / 86_400_000);
}

function daysAgo(iso: string | null): number | null {
  const u = daysUntil(iso);
  return u === null ? null : -u;
}

function EarningsThesisCell({
  h,
}: {
  h: ConsolidatedHoldingOut;
}): JSX.Element {
  const es = h.earnings;
  if (!es || (!es.tracked && !es.has_brief && es.next_earnings_date === null)) {
    return <span className="text-slate-400 text-[11px]">—</span>;
  }
  const dUntil = daysUntil(es.next_earnings_date);
  const earningsChipColor =
    dUntil === null
      ? "bg-slate-100 text-slate-600"
      : dUntil <= 7
        ? "bg-rose-100 text-rose-800"
        : dUntil <= 14
          ? "bg-amber-100 text-amber-800"
          : "bg-slate-100 text-slate-700";
  const thesisColor =
    es.thesis_status === "breach"
      ? "bg-rose-100 text-rose-800"
      : es.thesis_status === "ok"
        ? "bg-emerald-100 text-emerald-800"
        : "bg-slate-100 text-slate-600";
  return (
    <div
      className="flex flex-wrap items-center gap-1"
      onClick={(e) => e.stopPropagation()}
    >
      {es.next_earnings_date ? (
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${earningsChipColor}`}
          title={`Next earnings ${es.next_earnings_date}${dUntil !== null ? ` (${dUntil}d)` : ""}`}
        >
          {es.next_earnings_date.slice(5)}
        </span>
      ) : null}
      {es.thesis_status ? (
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${thesisColor}`}
          title={es.thesis_summary ?? `thesis status: ${es.thesis_status}`}
        >
          {es.thesis_status}
        </span>
      ) : null}
      {es.has_brief && h.ticker ? (() => {
        const age = daysAgo(es.latest_brief_iso_date);
        const briefClass =
          age === null
            ? "bg-indigo-100 text-indigo-800 hover:bg-indigo-200"
            : age <= 30
              ? "bg-indigo-100 text-indigo-800 hover:bg-indigo-200"
              : age <= 90
                ? "bg-amber-100 text-amber-800 hover:bg-amber-200"
                : "bg-rose-100 text-rose-800 hover:bg-rose-200";
        const ageLabel =
          age === null ? "" : age === 0 ? " · today" : ` · ${age}d`;
        return (
          <a
            href={`/api/earnings-summary/brief/${encodeURIComponent(h.ticker)}`}
            target="_blank"
            rel="noopener noreferrer"
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${briefClass}`}
            title={`Open latest research brief (${es.latest_brief_iso_date}${age !== null ? `, ${age}d ago` : ""})`}
          >
            brief{ageLabel}
          </a>
        );
      })() : null}
    </div>
  );
}

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
              <Th>Earnings / thesis</Th>
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
  // Aggregate per-account cost basis sources for the top-row badge.
  const sources = new Set(
    h.accounts.map((a) => a.cost_basis_source).filter(Boolean) as CostBasisSource[],
  );
  // Pick the most specific badge: prefer inferred over manual when both
  // are present (inferred is the more interesting case).
  const headlineSource: CostBasisSource | null =
    sources.has("inferred_acats")
      ? "inferred_acats"
      : sources.has("inferred_1099")
        ? "inferred_1099"
        : sources.has("manual")
          ? "manual"
          : null;

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
          <span className="inline-flex items-center gap-1">
            {fmtUSD(cost)}
            {headlineSource ? (
              <span
                className={`rounded px-1 py-0.5 text-[9px] uppercase tracking-wide ${SOURCE_CLASS[headlineSource]}`}
                title={`Cost basis is ${SOURCE_LABEL[headlineSource]} for at least one account (not broker-reported). Click the row to see per-account detail.`}
              >
                {headlineSource === "manual" ? "MAN" : "INF"}
              </span>
            ) : null}
          </span>
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
        <Td><EarningsThesisCell h={h} /></Td>
        <Td align="right" className="text-slate-500">
          {accountCount} {accountCount === 1 ? "account" : "accounts"}
        </Td>
      </tr>
      {open && (
        <tr className="bg-slate-50/60">
          <td colSpan={9} className="px-4 py-3">
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
                        <span className="inline-flex items-center gap-1">
                          {fmtUSD(aCost)}
                          {a.cost_basis_source ? (
                            <span
                              className={`rounded px-1 py-0.5 text-[9px] uppercase tracking-wide ${SOURCE_CLASS[a.cost_basis_source]}`}
                              title={`Source: ${SOURCE_LABEL[a.cost_basis_source]}. Not broker-reported.`}
                            >
                              {SOURCE_LABEL[a.cost_basis_source]}
                            </span>
                          ) : null}
                        </span>
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
