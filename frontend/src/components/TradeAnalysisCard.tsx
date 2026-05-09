import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import { Card, fmtUSD } from "@/components/ui";
import type { TickerTrade } from "@/types";

/**
 * Trade analysis + light coaching surface.
 *
 * Three sub-tables — winners, losers, currently held — plus a turnover
 * stat at the top. Sources `/api/portfolio/trade-analysis`. Methodology
 * lives on the backend; the UI just presents and groups.
 *
 * Caveats actively surfaced (so the user doesn't take individual rows
 * too literally):
 *   - Window-based: trades that closed before `start_date` aren't in P&L.
 *   - Put assignments show as forced buys (premium income not credited).
 *   - Untracked transfer-outs leave shares dangling — those positions
 *     show as "data incomplete" rather than as honest losers.
 *
 * The "Coaching framework" panel is static text, intentionally separate
 * from the data so the principles don't drift with each render.
 */
export function TradeAnalysisCard({
  startDate,
  endDate,
}: {
  startDate?: string;
  endDate?: string;
}): JSX.Element {
  const [view, setView] = useState<"winners" | "losers" | "open">("winners");

  const { data, isLoading, isError } = useQuery({
    queryKey: ["trade-analysis", { startDate, endDate }],
    queryFn: () => api.tradeAnalysis({ startDate, endDate }),
  });

  const tickers = data?.tickers ?? [];

  const winners = useMemo(
    () => tickers.filter((t) => parseFloat(t.pnl_dollars) > 0).slice(0, 12),
    [tickers],
  );
  const losers = useMemo(
    () => tickers.filter((t) => parseFloat(t.pnl_dollars) < 0).slice(0, 12),
    [tickers],
  );
  const open = useMemo(
    () => tickers.filter((t) => t.is_open).slice(0, 15),
    [tickers],
  );

  return (
    <Card>
      <header className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">
            Trade analysis &amp; coaching
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Per-ticker P&amp;L, trading activity, and a process checklist for
            future trades.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <ViewChip
            active={view === "winners"}
            onClick={() => setView("winners")}
            label={`Winners (${winners.length})`}
          />
          <ViewChip
            active={view === "losers"}
            onClick={() => setView("losers")}
            label={`Losers (${losers.length})`}
          />
          <ViewChip
            active={view === "open"}
            onClick={() => setView("open")}
            label={`Open (${open.length})`}
          />
        </div>
      </header>

      {isLoading ? (
        <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
      ) : isError || !data ? (
        <div className="px-4 py-6 text-xs text-red-700">
          Failed to load trade analysis.
        </div>
      ) : (
        <>
          <ActivityStrip activity={data.activity} />

          <TradeTable
            rows={
              view === "winners"
                ? winners
                : view === "losers"
                  ? losers
                  : open
            }
            kind={view}
          />

          {data.notes.length > 0 && (
            <ul className="border-t border-slate-100 bg-slate-50 px-4 py-3 text-xs text-slate-600 space-y-1.5">
              {data.notes.map((n, idx) => (
                <li key={idx} className="leading-relaxed">
                  · {n}
                </li>
              ))}
            </ul>
          )}

          <CoachingPanel />
        </>
      )}
    </Card>
  );
}

function ActivityStrip({
  activity,
}: {
  activity: {
    total_trades: number;
    total_notional: string;
    annualized_turnover_pct: number | null;
    average_position_value: string | null;
  };
}): JSX.Element {
  const turnover = activity.annualized_turnover_pct;
  const turnoverColor =
    turnover === null
      ? "text-slate-700"
      : turnover > 100
        ? "text-red-700"
        : turnover > 50
          ? "text-amber-700"
          : "text-emerald-700";
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-b border-slate-100 px-4 py-3 sm:grid-cols-4">
      <Stat label="Total trades" value={activity.total_trades.toLocaleString()} />
      <Stat
        label="Notional traded"
        value={fmtUSD(parseFloat(activity.total_notional))}
      />
      <Stat
        label="Annualized turnover"
        value={turnover !== null ? `${turnover.toFixed(0)}%` : "—"}
        valueClass={turnoverColor}
      />
      <Stat
        label="Avg position value"
        value={
          activity.average_position_value
            ? fmtUSD(parseFloat(activity.average_position_value))
            : "—"
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

function TradeTable({
  rows,
  kind,
}: {
  rows: TickerTrade[];
  kind: "winners" | "losers" | "open";
}): JSX.Element {
  if (rows.length === 0) {
    return (
      <div className="px-4 py-6 text-center text-xs text-slate-500">
        No {kind} in this window.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-slate-200">
        <thead className="bg-slate-50">
          <tr>
            <Th>Ticker</Th>
            <Th>First buy</Th>
            <Th>Last action</Th>
            <Th align="right">Bought</Th>
            <Th align="right">Sold</Th>
            <Th align="right">Today value</Th>
            <Th align="right">P&amp;L</Th>
            <Th align="right">P&amp;L %</Th>
            <Th align="right">#&nbsp;trades</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((r) => {
            const pnl = parseFloat(r.pnl_dollars);
            const cls = pnl > 0 ? "text-emerald-700" : pnl < 0 ? "text-red-700" : "";
            const pnlPct = r.pnl_pct ?? 0;
            // "Data incomplete" tag: bought without sells AND no current
            // position usually means an untracked ACATS-out.
            const incomplete =
              !r.is_open &&
              parseFloat(r.bought_total) > 0 &&
              parseFloat(r.sold_total) === 0;
            return (
              <tr key={r.ticker} className="hover:bg-slate-50">
                <Td>
                  <div className="font-mono font-semibold text-slate-900">
                    {r.ticker}
                  </div>
                  {incomplete && (
                    <div className="text-[10px] text-amber-700">
                      data incomplete
                    </div>
                  )}
                </Td>
                <Td className="text-xs text-slate-500 tabular-nums">
                  {r.first_buy ?? "—"}
                </Td>
                <Td className="text-xs text-slate-500 tabular-nums">
                  {r.last_action ?? "—"}
                </Td>
                <Td align="right" className="tabular-nums">
                  {fmtUSD(parseFloat(r.bought_total))}
                </Td>
                <Td align="right" className="tabular-nums">
                  {fmtUSD(parseFloat(r.sold_total))}
                </Td>
                <Td align="right" className="tabular-nums">
                  {parseFloat(r.today_value) > 0
                    ? fmtUSD(parseFloat(r.today_value))
                    : "—"}
                </Td>
                <Td align="right" className={`tabular-nums font-semibold ${cls}`}>
                  {pnl >= 0 ? "+" : "−"}
                  {fmtUSD(Math.abs(pnl))}
                </Td>
                <Td align="right" className={`tabular-nums ${cls}`}>
                  {r.pnl_pct !== null
                    ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(0)}%`
                    : "—"}
                </Td>
                <Td align="right" className="tabular-nums text-slate-500">
                  {r.trade_count}
                </Td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ViewChip({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "rounded px-2.5 py-1 text-xs font-medium transition-colors",
        active
          ? "bg-slate-900 text-white"
          : "text-slate-600 hover:bg-slate-100",
      ].join(" ")}
    >
      {label}
    </button>
  );
}

function Th({
  children,
  align,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}): JSX.Element {
  return (
    <th
      scope="col"
      className={[
        "px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-slate-500",
        align === "right" ? "text-right" : "text-left",
      ].join(" ")}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align,
  className,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  className?: string;
}): JSX.Element {
  return (
    <td
      className={[
        "px-3 py-2 text-sm text-slate-700",
        align === "right" ? "text-right" : "text-left",
        className ?? "",
      ].join(" ")}
    >
      {children}
    </td>
  );
}

/**
 * Static coaching panel — process principles, not market predictions.
 *
 * Lives in the same card as the data so the user reads it together with
 * their actual trades. Intentionally short — the goal is reminders the
 * user can scan in 30 seconds before their next trade, not a textbook.
 */
function CoachingPanel(): JSX.Element {
  return (
    <details className="border-t border-slate-100 bg-indigo-50/40 px-4 py-3">
      <summary className="cursor-pointer text-sm font-semibold text-slate-900 select-none">
        Process checklist for the next trade
      </summary>
      <div className="mt-3 grid grid-cols-1 gap-4 text-xs leading-relaxed text-slate-700 lg:grid-cols-3">
        <Section title="Before you click buy">
          <li>
            <strong>Write a 1-page thesis.</strong> What does the company do?
            Why is the market mispricing it? Specific catalyst? Time horizon?
            If you can't write it, don't buy it.
          </li>
          <li>
            <strong>Read primary sources.</strong> Latest 10-K, last 3 earnings
            transcripts, two competitor 10-Ks. Discount sell-side research.
          </li>
          <li>
            <strong>Build a rough valuation.</strong> DCF or multiples →
            range of fair value. Buy when price is below the low end.
          </li>
          <li>
            <strong>Pre-define invalidation triggers.</strong> Specific
            fundamental events (revenue growth, margin, regulatory) that
            would change your mind. <em>Not</em> "stock fell 20%."
          </li>
          <li>
            <strong>Size by conviction.</strong> Highest = up to 10%. Standard
            = 3–5%. Speculative = 1–2%.
          </li>
        </Section>
        <Section title="While you hold">
          <li>
            <strong>Read every quarterly report.</strong> Update thesis status:
            still valid? evolved? broken?
          </li>
          <li>
            <strong>Don't check daily price</strong> unless an invalidation
            trigger fired. Price is noise; fundamentals are signal.
          </li>
          <li>
            <strong>Maintain a journal.</strong> One page per position with
            thesis, current state, next checkpoint date.
          </li>
          <li>
            <strong>Quarterly review.</strong> Ask: would I buy this position
            today at the current price? If no, why hold it?
          </li>
        </Section>
        <Section title="When you sell">
          <li>
            <strong>Thesis fully realized.</strong> Target valuation hit, the
            re-rating you predicted happened.
          </li>
          <li>
            <strong>Thesis broken.</strong> Specific invalidation triggered.
          </li>
          <li>
            <strong>Better opportunity.</strong> Explicit IRR comparison —
            not "this looks cheaper."
          </li>
          <li className="text-red-700">
            <strong>NEVER:</strong> bad week, boredom, tip on something else,
            tax-loss harvesting at the cost of the thesis.
          </li>
          <li>
            <strong>Index ETFs (VTI/VOO/SPY) — buy and hold.</strong> No
            informational edge to trade them. Pick a target % and stick to it.
          </li>
        </Section>
      </div>
    </details>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div>
      <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-2">
        {title}
      </div>
      <ul className="list-disc pl-4 space-y-1.5">{children}</ul>
    </div>
  );
}
