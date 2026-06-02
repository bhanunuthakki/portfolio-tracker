import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { PositionAlphaResult } from "@/types";

export type BenchmarkKey = "spy" | "qqq" | "policy";

interface ChartRow {
  date: string;
  portfolio: number;
  spy: number;
  qqq: number;
  policy: number;
}

// Whole-dollar display per project convention (no decimal points on
// dollar amounts). Chart-axis abbreviations round to whole-thousands
// and whole-millions — precision lost at the M boundary is acceptable
// for axis labels; hover tooltips can show the full integer if needed.
const USD = (v: number, signed = false): string => {
  const sign = signed ? (v >= 0 ? "+" : "−") : "";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `${sign}$${Math.round(abs / 1_000_000)}M`;
  if (abs >= 1_000) return `${sign}$${Math.round(abs / 1_000)}k`;
  return `${sign}$${Math.round(abs)}`;
};

/**
 * Theme-aware chart palette. Series colors come from the editorial CSS
 * tokens (so the portfolio + SPY lines match the rest of the UI and flip
 * with the theme); QQQ/Policy use a small distinguishable qualitative set
 * since a strictly monochrome chart can't separate four lines. A
 * MutationObserver on <html data-theme> re-reads the tokens when the user
 * toggles light/dark — recharts only re-renders on prop/state change.
 */
interface Palette {
  grid: string;
  axis: string;
  ref: string;
  portfolio: string;
  spy: string;
  qqq: string;
  policy: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipText: string;
}

function readPalette(): Palette {
  const cs =
    typeof document !== "undefined"
      ? getComputedStyle(document.documentElement)
      : null;
  const v = (name: string, fallback: string): string => {
    const raw = cs?.getPropertyValue(name).trim();
    return raw ? `rgb(${raw})` : fallback;
  };
  const dark =
    typeof document !== "undefined" &&
    document.documentElement.dataset.theme === "dark";
  return {
    grid: v("--hairline", "#ecebe5"),
    axis: v("--muted", "#6c6f78"),
    ref: v("--faint", "#9a9da6"),
    portfolio: v("--ink", "#121318"),
    spy: v("--accent", "#1d4ed8"),
    qqq: dark ? "#b69cff" : "#6d4ed6",
    policy: dark ? "#5fd0bf" : "#0f7a6b",
    tooltipBg: v("--surface", "#ffffff"),
    tooltipBorder: v("--line", "#e2e1da"),
    tooltipText: v("--ink-soft", "#2f3239"),
  };
}

function usePalette(): Palette {
  const [pal, setPal] = useState<Palette>(readPalette);
  useEffect(() => {
    const obs = new MutationObserver(() => setPal(readPalette()));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => obs.disconnect();
  }, []);
  return pal;
}

const MONO =
  "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

export function PositionAlphaChart({
  data,
  visibleBenchmarks,
}: {
  data: PositionAlphaResult;
  visibleBenchmarks: Set<BenchmarkKey>;
}): JSX.Element {
  const pal = usePalette();

  if (data.series.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-line-strong bg-surface p-8 text-center text-sm text-muted">
        No data for this window.
      </div>
    );
  }

  const rows: ChartRow[] = data.series.map((p) => ({
    date: p.date,
    portfolio: parseFloat(p.portfolio_value),
    spy: parseFloat(p.spy_counterfactual_value),
    qqq: parseFloat(p.qqq_counterfactual_value),
    policy: parseFloat(p.policy_counterfactual_value),
  }));

  const valuesToConsider: number[] = rows.map((r) => r.portfolio);
  if (visibleBenchmarks.has("spy")) valuesToConsider.push(...rows.map((r) => r.spy));
  if (visibleBenchmarks.has("qqq")) valuesToConsider.push(...rows.map((r) => r.qqq));
  if (visibleBenchmarks.has("policy") && data.has_policy)
    valuesToConsider.push(...rows.map((r) => r.policy));
  const yMin = Math.min(...valuesToConsider);
  const yMax = Math.max(...valuesToConsider);
  const pad = (yMax - yMin) * 0.05 || 1;

  const tick = { fontSize: 11, fill: pal.axis, fontFamily: MONO };

  return (
    <div className="h-80">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 10, right: 16, bottom: 4, left: 8 }}>
          <CartesianGrid stroke={pal.grid} strokeDasharray="3 3" />
          <XAxis
            dataKey="date"
            tick={tick}
            minTickGap={32}
            stroke={pal.grid}
            tickLine={{ stroke: pal.grid }}
          />
          <YAxis
            tick={tick}
            domain={[yMin - pad, yMax + pad]}
            tickFormatter={(v: number) => USD(v)}
            width={64}
            stroke={pal.grid}
            tickLine={{ stroke: pal.grid }}
          />
          <Tooltip
            formatter={(value: number, name: string) => [USD(value), name]}
            labelStyle={{ fontSize: 12, color: pal.tooltipText, fontWeight: 600 }}
            contentStyle={{
              fontSize: 12,
              fontFamily: MONO,
              background: pal.tooltipBg,
              border: `1px solid ${pal.tooltipBorder}`,
              borderRadius: 8,
              boxShadow: "0 12px 40px rgb(0 0 0 / 0.12)",
              color: pal.tooltipText,
            }}
            cursor={{ stroke: pal.ref, strokeDasharray: "3 3" }}
          />
          <ReferenceLine
            y={parseFloat(data.v_start)}
            stroke={pal.ref}
            strokeDasharray="2 2"
            label={{
              value: `Start: ${USD(parseFloat(data.v_start))}`,
              fontSize: 10,
              fontFamily: MONO,
              fill: pal.axis,
              position: "insideTopLeft",
            }}
          />
          <Legend wrapperStyle={{ fontSize: 12, color: pal.axis }} />
          <Line
            type="monotone"
            dataKey="portfolio"
            name="Portfolio $"
            stroke={pal.portfolio}
            strokeWidth={2.25}
            dot={false}
            isAnimationActive={false}
          />
          {visibleBenchmarks.has("spy") && (
            <Line
              type="monotone"
              dataKey="spy"
              name="SPY $"
              stroke={pal.spy}
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          )}
          {visibleBenchmarks.has("qqq") && (
            <Line
              type="monotone"
              dataKey="qqq"
              name="QQQ $"
              stroke={pal.qqq}
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          )}
          {visibleBenchmarks.has("policy") && data.has_policy && (
            <Line
              type="monotone"
              dataKey="policy"
              name="Policy $"
              stroke={pal.policy}
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          )}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
