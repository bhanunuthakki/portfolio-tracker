import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import {
  filterBySearch,
  useTableSort,
} from "@/components/useTableSort";
import { Card, fmtUSD, pnlClass } from "@/components/ui";
import type { TickerTrade } from "@/types";

/**
 * Standalone Trade Analysis page with sortable columns + search filter.
 *
 * Replaces the dashboard card. Same /api/portfolio/trade-analysis source
 * but with full sort/search and a wider table. The Winners / Losers / Open
 * tabs still gate which subset of tickers is in scope.
 */

type View = "winners" | "losers" | "open" | "all";
type Preset = "6M" | "1Y" | "2Y" | "5Y" | "MAX";

const PRESETS: { key: Preset; label: string; days: number | null }[] = [
  { key: "6M", label: "6M", days: 180 },
  { key: "1Y", label: "1Y", days: 365 },
  { key: "2Y", label: "2Y", days: 730 },
  { key: "5Y", label: "5Y", days: 1825 },
  { key: "MAX", label: "Max", days: null },
];

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export function TradeAnalysis(): JSX.Element {
  const [view, setView] = useState<View>("all");
  const [preset, setPreset] = useState<Preset>("2Y");
  const [search, setSearch] = useState("");

  const startDate = useMemo(() => {
    const p = PRESETS.find((x) => x.key === preset);
    return p?.days ? isoDaysAgo(p.days) : undefined;
  }, [preset]);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["trade-analysis", { startDate, page: "trade-analysis" }],
    queryFn: () => api.tradeAnalysis({ startDate }),
  });

  const tickers = data?.tickers ?? [];
  const viewFiltered = useMemo(() => {
    if (view === "winners")
      return tickers.filter((t) => parseFloat(t.pnl_dollars) > 0);
    if (view === "losers")
      return tickers.filter((t) => parseFloat(t.pnl_dollars) < 0);
    if (view === "open") return tickers.filter((t) => t.is_open);
    return tickers;
  }, [tickers, view]);

  const searchFiltered = useMemo(
    () =>
      filterBySearch(viewFiltered, search, [
        (t) => t.ticker,
        (t) => t.name,
        (t) => t.first_buy,
        (t) => t.last_action,
      ]),
    [viewFiltered, search],
  );

  const accessors = useMemo(
    () => ({
      ticker: (t: TickerTrade) => t.ticker,
      name: (t: TickerTrade) => t.name ?? "",
      first_buy: (t: TickerTrade) => t.first_buy ?? "",
      last_action: (t: TickerTrade) => t.last_action ?? "",
      bought: (t: TickerTrade) => parseFloat(t.bought_total),
      sold: (t: TickerTrade) => parseFloat(t.sold_total),
      qty: (t: TickerTrade) => parseFloat(t.today_qty),
      value: (t: TickerTrade) => parseFloat(t.today_value),
      pnl: (t: TickerTrade) => parseFloat(t.pnl_dollars),
      pnl_pct: (t: TickerTrade) => (t.pnl_pct ?? -Infinity),
      trade_count: (t: TickerTrade) => t.trade_count,
    }),
    [],
  );

  const { sortedRows, toggle, sortIndicator } = useTableSort(
    searchFiltered,
    "pnl",
    "desc",
    accessors,
  );

  const totals = useMemo(() => {
    let wins = 0,
      losses = 0,
      open = 0;
    let pnlSum = 0;
    searchFiltered.forEach((t) => {
      const pnl = parseFloat(t.pnl_dollars);
      pnlSum += pnl;
      if (pnl > 0) wins++;
      else if (pnl < 0) losses++;
      if (t.is_open) open++;
    });
    return { wins, losses, open, pnlSum };
  }, [searchFiltered]);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold text-slate-900">
          Trade analysis
        </h1>
        <p className="mt-0.5 text-sm text-slate-500">
          Per-ticker buy/sell summary. Click any header to sort; type to
          filter by ticker, name, or date.
        </p>
      </header>

      <Card>
        <div className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-1">
            {(["all", "winners", "losers", "open"] as View[]).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                className={`rounded px-2 py-1 text-[11px] font-medium capitalize ${
                  view === v
                    ? "bg-slate-900 text-white"
                    : "bg-slate-100 text-slate-700 hover:bg-slate-200"
                }`}
              >
                {v}
              </button>
            ))}
            <span className="mx-2 text-slate-300">|</span>
            {PRESETS.map((p) => (
              <button
                key={p.key}
                type="button"
                onClick={() => setPreset(p.key)}
                className={`rounded px-2 py-1 text-[11px] font-medium ${
                  preset === p.key
                    ? "bg-slate-200 text-slate-900"
                    : "text-slate-600 hover:bg-slate-100"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <input
            type="search"
            placeholder="Ticker, name, or date…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded border border-slate-200 px-2 py-1 text-xs sm:w-72"
          />
        </div>

        {isLoading ? (
          <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
        ) : isError ? (
          <div className="px-4 py-6 text-xs text-red-700">Failed to load.</div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-b border-slate-100 px-4 py-3 sm:grid-cols-4">
              <StatBlock
                label="Total P&L (filtered)"
                value={fmtUSD(totals.pnlSum)}
                valueClass={pnlClass(totals.pnlSum)}
              />
              <StatBlock label="Winners" value={`${totals.wins}`} valueClass="text-emerald-700" />
              <StatBlock label="Losers" value={`${totals.losses}`} valueClass="text-rose-700" />
              <StatBlock label="Currently open" value={`${totals.open}`} />
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-slate-50 text-slate-600">
                  <tr>
                    <Sh onClick={() => toggle("ticker")}>
                      Ticker{sortIndicator("ticker")}
                    </Sh>
                    <Sh onClick={() => toggle("name")}>
                      Name{sortIndicator("name")}
                    </Sh>
                    <Sh onClick={() => toggle("first_buy")}>
                      First buy{sortIndicator("first_buy")}
                    </Sh>
                    <Sh onClick={() => toggle("last_action")}>
                      Last action{sortIndicator("last_action")}
                    </Sh>
                    <Sh align="right" onClick={() => toggle("bought")}>
                      Bought{sortIndicator("bought")}
                    </Sh>
                    <Sh align="right" onClick={() => toggle("sold")}>
                      Sold{sortIndicator("sold")}
                    </Sh>
                    <Sh align="right" onClick={() => toggle("qty")}>
                      Today qty{sortIndicator("qty")}
                    </Sh>
                    <Sh align="right" onClick={() => toggle("value")}>
                      Today value{sortIndicator("value")}
                    </Sh>
                    <Sh align="right" onClick={() => toggle("pnl")}>
                      P&amp;L $ {sortIndicator("pnl")}
                    </Sh>
                    <Sh align="right" onClick={() => toggle("pnl_pct")}>
                      P&amp;L %{sortIndicator("pnl_pct")}
                    </Sh>
                    <Sh align="right" onClick={() => toggle("trade_count")}>
                      Trades{sortIndicator("trade_count")}
                    </Sh>
                    <Sh onClick={() => undefined}>Earnings / thesis</Sh>
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.length === 0 ? (
                    <tr>
                      <td colSpan={12} className="px-4 py-6 text-center text-slate-500">
                        No matching rows.
                      </td>
                    </tr>
                  ) : (
                    sortedRows.map((t) => {
                      const pnl = parseFloat(t.pnl_dollars);
                      return (
                        <tr
                          key={t.ticker}
                          className="border-t border-slate-100 hover:bg-slate-50"
                        >
                          <td className="px-3 py-1.5 font-mono text-slate-900">
                            {t.ticker}
                            {t.cost_basis_unreliable ? (
                              <span
                                className="ml-1 text-amber-600"
                                title="Cost basis unreliable (ACATS / pre-history)"
                              >
                                ⚠
                              </span>
                            ) : null}
                          </td>
                          <td className="px-3 py-1.5 text-slate-700 max-w-[200px]">
                            <span className="block truncate" title={t.name ?? ""}>
                              {t.name ?? "—"}
                            </span>
                          </td>
                          <td className="px-3 py-1.5 tabular-nums text-slate-600">
                            {t.first_buy ?? "—"}
                          </td>
                          <td className="px-3 py-1.5 tabular-nums text-slate-600">
                            {t.last_action ?? "—"}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                            {fmtUSD(parseFloat(t.bought_total))}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                            {fmtUSD(parseFloat(t.sold_total))}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                            {parseFloat(t.today_qty).toLocaleString(undefined, {
                              maximumFractionDigits: 3,
                            })}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                            {fmtUSD(parseFloat(t.today_value))}
                          </td>
                          <td className={`px-3 py-1.5 text-right font-semibold tabular-nums ${pnlClass(pnl)}`}>
                            {fmtUSD(pnl)}
                          </td>
                          <td className={`px-3 py-1.5 text-right tabular-nums ${pnlClass(t.pnl_pct)}`}>
                            {t.pnl_pct !== null
                              ? `${(t.pnl_pct * 100).toFixed(1)}%`
                              : "—"}
                          </td>
                          <td className="px-3 py-1.5 text-right tabular-nums text-slate-600">
                            {t.trade_count}
                          </td>
                          <td className="px-3 py-1.5">
                            <EarningsThesisChips trade={t} />
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
            <p className="border-t border-slate-100 bg-slate-50 px-4 py-2 text-xs text-slate-500">
              Showing {sortedRows.length} of {viewFiltered.length} tickers.
              {data && data.notes.length > 0 && ` · ${data.notes[0]}`}
            </p>
          </>
        )}
      </Card>
    </div>
  );
}

function EarningsThesisChips({ trade }: { trade: TickerTrade }): JSX.Element {
  const hasAny =
    trade.es_tracked || trade.es_next_earnings_date || trade.es_has_brief;
  if (!hasAny) return <span className="text-slate-400 text-[11px]">—</span>;
  const dUntil = trade.es_next_earnings_date
    ? Math.round(
        (new Date(trade.es_next_earnings_date + "T00:00:00Z").getTime() -
          new Date(new Date().toISOString().slice(0, 10) + "T00:00:00Z").getTime()) /
          86_400_000,
      )
    : null;
  const earningsCls =
    dUntil === null
      ? "bg-slate-100 text-slate-600"
      : dUntil <= 7
        ? "bg-rose-100 text-rose-800"
        : dUntil <= 14
          ? "bg-amber-100 text-amber-800"
          : "bg-slate-100 text-slate-700";
  const thesisCls =
    trade.es_thesis_status === "breach"
      ? "bg-rose-100 text-rose-800"
      : trade.es_thesis_status === "ok"
        ? "bg-emerald-100 text-emerald-800"
        : "bg-slate-100 text-slate-600";
  return (
    <div className="flex flex-wrap items-center gap-1">
      {trade.es_next_earnings_date ? (
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${earningsCls}`}
          title={`Next earnings ${trade.es_next_earnings_date} (${dUntil}d)`}
        >
          {trade.es_next_earnings_date.slice(5)}
        </span>
      ) : null}
      {trade.es_thesis_status ? (
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${thesisCls}`}
          title={`thesis status: ${trade.es_thesis_status}`}
        >
          {trade.es_thesis_status}
        </span>
      ) : null}
      {trade.es_has_brief ? (
        <a
          href={`/api/earnings-summary/brief/${encodeURIComponent(trade.ticker)}`}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-800 hover:bg-indigo-200"
          title={`Open latest research brief (${trade.es_latest_brief_iso_date})`}
        >
          brief
        </a>
      ) : null}
    </div>
  );
}

function Sh({
  children,
  align,
  onClick,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  onClick: () => void;
}): JSX.Element {
  return (
    <th
      onClick={onClick}
      className={[
        "cursor-pointer select-none px-3 py-2 text-xs font-medium uppercase tracking-wide text-slate-600 hover:bg-slate-100",
        align === "right" ? "text-right" : "text-left",
      ].join(" ")}
    >
      {children}
    </th>
  );
}

function StatBlock({
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
