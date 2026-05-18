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

interface ChartRow {
  date: string;
  portfolio: number;
  spy: number;
}

const USD = (v: number, signed = false): string => {
  const sign = signed ? (v >= 0 ? "+" : "−") : "";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 10_000) return `${sign}$${(abs / 1_000).toFixed(0)}k`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
};

export function PositionAlphaChart({ data }: { data: PositionAlphaResult }): JSX.Element {
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
  }));

  // Find the y-axis range so both lines fit
  const allVals = rows.flatMap((r) => [r.portfolio, r.spy]);
  const yMin = Math.min(...allVals);
  const yMax = Math.max(...allVals);
  const pad = (yMax - yMin) * 0.05;

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
            formatter={(value: number, name: string) => [
              USD(value),
              name,
            ]}
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
            name="Active positions $"
            stroke="#0f172a"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="spy"
            name="SPY counterfactual $"
            stroke="#2563eb"
            strokeWidth={1.75}
            dot={false}
            isAnimationActive={false}
            strokeDasharray="0"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
