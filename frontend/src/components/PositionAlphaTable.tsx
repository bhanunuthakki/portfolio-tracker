import type {
  ConsolidatedHoldingOut,
  PositionAlphaResult,
  PositionAlphaRow,
} from "@/types";

const fmtUSD = (v: number, signed = false): string => {
  const sign = signed && v > 0 ? "+" : "";
  return `${sign}$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
};

const fmtMinus = (v: number): string => {
  if (v >= 0) return `+$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  return `−$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
};

const colorCls = (v: number): string =>
  v > 0 ? "text-emerald-700" : v < 0 ? "text-rose-700" : "text-slate-500";

export function PositionAlphaTable({
  data,
  holdings = [],
}: {
  data: PositionAlphaResult;
  holdings?: ConsolidatedHoldingOut[];
}): JSX.Element {
  const rows = [...data.rows].sort(
    (a, b) => parseFloat(a.alpha) - parseFloat(b.alpha),
  );
  const totalActual = parseFloat(data.total_actual_pl);
  const totalSpy = parseFloat(data.total_spy_pl);
  const totalAlpha = parseFloat(data.total_alpha);

  // Map ticker -> reliability info from current holdings. A ticker is
  // 'unreliable' when at least one contributing account has implausibly
  // low broker-reported cost basis (the UNREL flag from Holdings).
  const unreliableByTicker = new Map<string, boolean>();
  for (const h of holdings) {
    if (h.ticker) {
      unreliableByTicker.set(h.ticker.toUpperCase(), h.has_unreliable_cost_basis);
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-xs">
        <thead className="bg-slate-50 text-slate-600">
          <tr>
            <th className="px-3 py-2 text-left font-medium">Ticker</th>
            <th className="px-3 py-2 text-right font-medium">V start</th>
            <th className="px-3 py-2 text-right font-medium">Bought</th>
            <th className="px-3 py-2 text-right font-medium">Sold</th>
            <th className="px-3 py-2 text-right font-medium">V end</th>
            <th className="px-3 py-2 text-right font-medium">Actual P&amp;L</th>
            <th className="px-3 py-2 text-right font-medium">SPY counterfactual</th>
            <th className="px-3 py-2 text-right font-medium">Alpha</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((r: PositionAlphaRow) => {
            const alpha = parseFloat(r.alpha);
            const actual = parseFloat(r.actual_pl);
            const spy = parseFloat(r.spy_counterfactual_pl);
            return (
              <tr key={r.ticker} className="hover:bg-slate-50">
                <td className="px-3 py-1.5 font-mono font-medium text-slate-900">
                  {r.ticker}
                  {r.incomplete && (
                    <span
                      className="ml-1 rounded bg-amber-100 px-1 py-0.5 text-[9px] font-medium text-amber-800"
                      title="Missing price data for window start or end — alpha approximate"
                    >
                      ⚠
                    </span>
                  )}
                  {unreliableByTicker.get(r.ticker) && (
                    <span
                      className="ml-1 rounded bg-rose-100 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-rose-800"
                      title="At least one account has implausible broker-reported cost basis for this ticker. Cost-basis-derived numbers may be off; alpha is still meaningful since position-alpha uses qty × price, not cost basis. See Holdings drill-down to set a manual override."
                    >
                      UNREL
                    </span>
                  )}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                  {fmtUSD(parseFloat(r.value_at_start))}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                  {fmtUSD(parseFloat(r.bought_in_window))}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                  {fmtUSD(parseFloat(r.sold_in_window))}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                  {fmtUSD(parseFloat(r.value_at_end))}
                </td>
                <td className={`px-3 py-1.5 text-right tabular-nums ${colorCls(actual)}`}>
                  {fmtMinus(actual)}
                </td>
                <td className={`px-3 py-1.5 text-right tabular-nums ${colorCls(spy)}`}>
                  {fmtMinus(spy)}
                </td>
                <td className={`px-3 py-1.5 text-right tabular-nums font-semibold ${colorCls(alpha)}`}>
                  {fmtMinus(alpha)}
                </td>
              </tr>
            );
          })}
        </tbody>
        <tfoot className="bg-slate-50 font-medium">
          <tr>
            <td className="px-3 py-2 text-left">Total</td>
            <td className="px-3 py-2 text-right tabular-nums text-slate-700">
              {fmtUSD(parseFloat(data.v_start))}
            </td>
            <td className="px-3 py-2 text-right tabular-nums text-slate-500" colSpan={2}>
              {" "}
            </td>
            <td className="px-3 py-2 text-right tabular-nums text-slate-700">
              {fmtUSD(parseFloat(data.v_end))}
            </td>
            <td className={`px-3 py-2 text-right tabular-nums ${colorCls(totalActual)}`}>
              {fmtMinus(totalActual)}
            </td>
            <td className={`px-3 py-2 text-right tabular-nums ${colorCls(totalSpy)}`}>
              {fmtMinus(totalSpy)}
            </td>
            <td className={`px-3 py-2 text-right tabular-nums font-bold ${colorCls(totalAlpha)}`}>
              {fmtMinus(totalAlpha)}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
