import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import { DecisionLogCard } from "@/components/DecisionLogCard";
import { EarningsCalendarCard } from "@/components/EarningsCalendarCard";
import { PerformanceChart } from "@/components/PerformanceChart";
import { RiskMetricsCard } from "@/components/RiskMetricsCard";
import { TradeAnalysisCard } from "@/components/TradeAnalysisCard";
import { Card, ErrorBanner, Stat } from "@/components/ui";

type RangePreset = "1M" | "3M" | "6M" | "YTD" | "1Y" | "2Y" | "MAX" | "CUSTOM";

const PRESETS: { key: Exclude<RangePreset, "CUSTOM">; label: string }[] = [
  { key: "1M", label: "1M" },
  { key: "3M", label: "3M" },
  { key: "6M", label: "6M" },
  { key: "YTD", label: "YTD" },
  { key: "1Y", label: "1Y" },
  { key: "2Y", label: "2Y" },
  { key: "MAX", label: "Max" },
];

// Dollar amount of "untouchable" emergency cash reserve. When the user
// toggles "Exclude reserve" on the dashboard, this much is carved off the
// top of V_start, every daily V, AND each synthetic-benchmark base before
// returns are computed — so the chart compares the *investable* portion
// against an equivalent-sized index allocation.
const CASH_RESERVE_AMOUNT = 30000;

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

function isoYTD(): string {
  const d = new Date();
  return `${d.getUTCFullYear()}-01-01`;
}

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function presetToDates(preset: Exclude<RangePreset, "CUSTOM">): {
  startDate?: string;
  includeBackfill: boolean;
} {
  switch (preset) {
    case "1M":
      return { startDate: isoDaysAgo(30), includeBackfill: true };
    case "3M":
      return { startDate: isoDaysAgo(90), includeBackfill: true };
    case "6M":
      return { startDate: isoDaysAgo(180), includeBackfill: true };
    case "YTD":
      return { startDate: isoYTD(), includeBackfill: true };
    case "1Y":
      return { startDate: isoDaysAgo(365), includeBackfill: true };
    case "2Y":
      return { startDate: isoDaysAgo(730), includeBackfill: true };
    case "MAX":
      return { includeBackfill: true };
  }
}

export function Dashboard(): JSX.Element {
  const [preset, setPreset] = useState<RangePreset>("1Y");
  const [customStart, setCustomStart] = useState<string>(isoDaysAgo(365));
  const [customEnd, setCustomEnd] = useState<string>(todayISO());
  const [excludeReserve, setExcludeReserve] = useState<boolean>(false);
  const [excludeIndexEtfs, setExcludeIndexEtfs] = useState<boolean>(false);

  const params = useMemo(() => {
    if (preset === "CUSTOM") {
      return {
        startDate: customStart,
        endDate: customEnd,
        includeBackfill: true,
      };
    }
    return presetToDates(preset);
  }, [preset, customStart, customEnd]);

  const reserveAmount = excludeReserve ? CASH_RESERVE_AMOUNT : 0;

  const performance = useQuery({
    queryKey: ["performance", params, reserveAmount, excludeIndexEtfs],
    queryFn: () =>
      api.performance({
        startDate: params.startDate,
        endDate: params.endDate,
        includeBackfill: params.includeBackfill,
        reserveAmount,
        excludeIndexEtfs,
      }),
  });
  const holdings = useQuery({
    queryKey: ["holdings"],
    queryFn: () => api.latestHoldings(),
  });

  const totalValue = holdings.data
    ? holdings.data.reduce(
        (sum, h) => sum + (h.total_value !== null ? parseFloat(h.total_value) : 0),
        0,
      )
    : null;

  const cashflowIn = performance.data
    ? parseFloat(performance.data.net_external_cashflow_in)
    : null;
  const baseValue = performance.data ? parseFloat(performance.data.base_value) : null;
  const lastPoint =
    performance.data && performance.data.points.length > 0
      ? performance.data.points[performance.data.points.length - 1]
      : null;
  const portfolioReturn = lastPoint
    ? parseFloat(lastPoint.portfolio_return_pct)
    : null;
  const spyReturn =
    lastPoint && lastPoint.spy_return_pct !== null
      ? parseFloat(lastPoint.spy_return_pct)
      : null;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label="Portfolio value"
          value={
            totalValue !== null
              ? `$${totalValue.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
              : "—"
          }
        />
        <Stat
          label="Net contributions (window)"
          value={
            cashflowIn !== null
              ? `${cashflowIn >= 0 ? "+" : "-"}$${Math.round(Math.abs(cashflowIn)).toLocaleString()}`
              : "—"
          }
        />
        <Stat
          label="Return vs SPY"
          value={
            portfolioReturn !== null && spyReturn !== null
              ? `${portfolioReturn >= 0 ? "+" : ""}${portfolioReturn.toFixed(1)}%  vs  ${spyReturn >= 0 ? "+" : ""}${spyReturn.toFixed(1)}%`
              : portfolioReturn !== null
                ? `${portfolioReturn >= 0 ? "+" : ""}${portfolioReturn.toFixed(1)}%`
                : "—"
          }
          mono
        />
        <Stat
          label="Range"
          value={
            performance.data
              ? `${performance.data.start_date} → ${performance.data.end_date}`
              : "—"
          }
          mono
        />
      </div>

      {performance.data?.backfill_start_unreliable && (
        <BackfillWarning
          baseValue={baseValue}
          endValue={totalValue}
          earliestObserved={performance.data.earliest_observed_date}
        />
      )}

      <Card>
        <header className="flex flex-col gap-3 border-b border-slate-100 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
          <h2 className="text-sm font-semibold text-slate-900 shrink-0">
            Performance vs benchmarks
            {(excludeReserve || excludeIndexEtfs) && (
              <span className="ml-2 rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-800 align-middle">
                {excludeIndexEtfs ? "active picks only" : "ex-reserve"}
              </span>
            )}
          </h2>
          <div className="flex flex-wrap items-center gap-2 lg:flex-1 lg:justify-center lg:px-4">
            <ToggleChip
              active={excludeReserve}
              onClick={() => setExcludeReserve(!excludeReserve)}
              activeColor="emerald"
              label="Excl. $30k SGOV reserve"
              tooltip="Treat $30k of cash as untouchable emergency reserves; carve from V_start, every daily V, and the synthetic-benchmark base before computing returns."
            />
            <ToggleChip
              active={excludeIndexEtfs}
              onClick={() => setExcludeIndexEtfs(!excludeIndexEtfs)}
              activeColor="indigo"
              label="Excl. broad-index ETFs"
              tooltip="Strip VTI/VOO/SPY/IVV/RSP from V and add their buy/sell flows to the cashflow series. Isolates active stock-picking alpha."
            />
          </div>
          <div className="flex flex-wrap items-center gap-1 shrink-0">
            {PRESETS.map((p) => (
              <RangeChip
                key={p.key}
                label={p.label}
                active={preset === p.key}
                onClick={() => setPreset(p.key)}
              />
            ))}
            <RangeChip
              label="Custom"
              active={preset === "CUSTOM"}
              onClick={() => setPreset("CUSTOM")}
            />
          </div>
        </header>

        {preset === "CUSTOM" && (
          <div className="flex flex-wrap items-end gap-3 px-4 py-3 border-b border-slate-100 bg-slate-50/60">
            <label className="flex flex-col text-xs text-slate-600">
              Start date
              <input
                type="date"
                value={customStart}
                onChange={(e) => setCustomStart(e.target.value)}
                max={customEnd}
                className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
              />
            </label>
            <label className="flex flex-col text-xs text-slate-600">
              End date
              <input
                type="date"
                value={customEnd}
                onChange={(e) => setCustomEnd(e.target.value)}
                min={customStart}
                max={todayISO()}
                className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
              />
            </label>
          </div>
        )}

        <div className="px-4 py-3">
          {performance.isLoading ? (
            <div className="text-xs text-slate-500">Loading…</div>
          ) : performance.isError ? (
            <ErrorBanner>Failed to load performance series.</ErrorBanner>
          ) : performance.data ? (
            <PerformanceChart series={performance.data} />
          ) : null}
        </div>
      </Card>

      <RiskMetricsCard
        startDate={params.startDate}
        endDate={params.endDate}
        includeBackfill={params.includeBackfill}
        excludeIndexEtfs={excludeIndexEtfs}
        reserveAmount={reserveAmount}
      />

      <TradeAnalysisCard
        startDate={params.startDate}
        endDate={params.endDate}
      />

      <DecisionLogCard />

      <EarningsCalendarCard />
    </div>
  );
}

function BackfillWarning({
  baseValue,
  endValue,
  earliestObserved,
}: {
  baseValue: number | null;
  endValue: number | null;
  earliestObserved: string | null;
}): JSX.Element {
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
      <div className="font-medium">
        ⚠ The portfolio % line over this window is unreliable.
      </div>
      <p className="mt-1 text-xs leading-relaxed">
        The starting value is reconstructed by walking your transaction
        history backward (Plaid only retains 24 months). The reconstruction
        tracks positions but not cash, so it collapses the apparent starting
        portfolio to{" "}
        {baseValue !== null
          ? `$${Math.round(baseValue).toLocaleString()}`
          : "an unknown amount"}{" "}
        when the actual end value is{" "}
        {endValue !== null ? `$${Math.round(endValue).toLocaleString()}` : "—"}
        . With a tiny start, every dollar of gain reads as a huge percentage.
      </p>
      <p className="mt-2 text-xs leading-relaxed">
        The SPY / QQQ <em>matched-flow</em> lines use the same wrong start,
        so they understate too — but proportionally less, because synthetic
        index portfolios grow at known market rates. The <strong>relative
        ordering</strong> (portfolio vs index) is correct; the <strong>absolute
        gap</strong> is exaggerated.
      </p>
      <p className="mt-2 text-xs leading-relaxed">
        <strong>Fully trustworthy:</strong> current portfolio value,
        contributions total, raw SPY / QQQ market returns. <strong>Becomes
        trustworthy:</strong> the portfolio % line{" "}
        {earliestObserved
          ? `from ${earliestObserved} forward`
          : "once daily snapshots accumulate"}
        . Schedule the snapshot job nightly.
      </p>
    </div>
  );
}

function RangeChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
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

/**
 * Pill-styled toggle that fills the white space between the chart title
 * and the range-preset chips. Two of these sit side-by-side on the
 * dashboard — one for the SGOV cash-reserve carve-out, one for the
 * broad-index ETF carve-out. Active state reads as a colored pill so the
 * effect on the chart is unambiguous when scanning quickly.
 *
 * `tooltip` ends up on the native `title` attribute — short, hover-only,
 * doesn't compete with the page layout. Click target is large enough to
 * comfortably hit on touch (≥32px tall via py-1.5 + text + border).
 */
function ToggleChip({
  active,
  onClick,
  activeColor,
  label,
  tooltip,
}: {
  active: boolean;
  onClick: () => void;
  activeColor: "emerald" | "indigo";
  label: string;
  tooltip?: string;
}): JSX.Element {
  const activeClasses =
    activeColor === "emerald"
      ? "border-emerald-600 bg-emerald-50 text-emerald-800 hover:bg-emerald-100"
      : "border-indigo-600 bg-indigo-50 text-indigo-800 hover:bg-indigo-100";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={active}
      onClick={onClick}
      title={tooltip}
      className={[
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors",
        active
          ? activeClasses
          : "border-slate-300 bg-white text-slate-600 hover:border-slate-400 hover:bg-slate-50",
      ].join(" ")}
    >
      <span
        aria-hidden="true"
        className={[
          "inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border",
          active
            ? activeColor === "emerald"
              ? "border-emerald-600 bg-emerald-600 text-white"
              : "border-indigo-600 bg-indigo-600 text-white"
            : "border-slate-400 bg-white",
        ].join(" ")}
      >
        {active ? "✓" : ""}
      </span>
      <span>{label}</span>
    </button>
  );
}
