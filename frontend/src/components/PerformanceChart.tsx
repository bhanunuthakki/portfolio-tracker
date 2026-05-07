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

import type { PerformanceSeries } from "@/types";

interface ChartRow {
  date: string;
  portfolio: number | null;
  spy: number | null;
  qqq: number | null;
}

export function PerformanceChart({ series }: { series: PerformanceSeries }): JSX.Element {
  if (series.points.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
        No data yet. Link an account on the Accounts page, then run the snapshotter.
      </div>
    );
  }

  const data: ChartRow[] = series.points.map((p) => ({
    date: p.date,
    portfolio: parseFloat(p.portfolio_return_pct),
    spy: p.spy_return_pct !== null ? parseFloat(p.spy_return_pct) : null,
    qqq: p.qqq_return_pct !== null ? parseFloat(p.qqq_return_pct) : null,
  }));

  return (
    <div>
      <div className="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 10, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="#e2e8f0" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={32} />
            <YAxis
              tick={{ fontSize: 11 }}
              domain={["auto", "auto"]}
              tickFormatter={(v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(0)}%`}
            />
            <Tooltip
              formatter={(value: number) =>
                typeof value === "number" ? `${value >= 0 ? "+" : ""}${value.toFixed(1)}%` : value
              }
            />
            <ReferenceLine y={0} stroke="#94a3b8" strokeDasharray="2 2" />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line
              type="monotone"
              dataKey="portfolio"
              name="Portfolio"
              stroke="#0f172a"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="spy"
              name="SPY (matched flows)"
              stroke="#2563eb"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="qqq"
              name="QQQ (matched flows)"
              stroke="#9333ea"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        % return uses Modified Dietz with money-flow matching. The SPY and QQQ
        lines are <em>synthetic portfolios</em> with the same starting value
        and the same contribution dates / amounts as yours, but invested in
        the index. The gap between your line and theirs is true relative
        performance — V<sub>start</sub> bias cancels in the comparison.
      </p>
    </div>
  );
}
