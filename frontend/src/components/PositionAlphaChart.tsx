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

export function PositionAlphaChart({
  data,
  visibleBenchmarks,
}: {
  data: PositionAlphaResult;
  visibleBenchmarks: Set<BenchmarkKey>;
}): JSX.Element {
  if (data.series.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
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

  return (
    <div className="h-80">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 10, right: 16, bottom: 4, left: 8 }}>
          <CartesianGrid stroke="#e2e8f0" strokeDasharray="3 3" />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={32} />
          <YAxis
            tick={{ fontSize: 11 }}
            domain={[yMin - pad, yMax + pad]}
            tickFormatter={(v: number) => USD(v)}
            width={64}
          />
          <Tooltip
            formatter={(value: number, name: string) => [USD(value), name]}
            labelStyle={{ fontSize: 12 }}
            contentStyle={{ fontSize: 12 }}
          />
          <ReferenceLine
            y={parseFloat(data.v_start)}
            stroke="#94a3b8"
            strokeDasharray="2 2"
            label={{
              value: `Start: ${USD(parseFloat(data.v_start))}`,
              fontSize: 10,
              fill: "#64748b",
              position: "insideTopLeft",
            }}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line
            type="monotone"
            dataKey="portfolio"
            name="Portfolio $"
            stroke="#0f172a"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          {visibleBenchmarks.has("spy") && (
            <Line
              type="monotone"
              dataKey="spy"
              name="SPY $"
              stroke="#2563eb"
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
              stroke="#9333ea"
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
              stroke="#0d9488"
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
