import { InfoButton } from "@/components/ui";
import type {
  ConsolidatedHoldingOut,
  PositionAlphaResult,
  PositionAlphaRow,
} from "@/types";

const POSITION_ALPHA_EXPLAINER = {
  definition:
    "Per-ticker dollar alpha vs SPY for the chosen window. We anchor V_start to qty × price on the window start date, then dollar-match every in-window buy/sell against the SPY counterfactual. Pre-window cost basis is ignored — only what you controlled at the window start matters.",
  formula:
    "alpha = (V_end + sold − bought) − (V_end_spy + sold − bought) = V_end − V_end_spy",
  interpretation:
    "Positive alpha = the position outperformed an equivalent SPY allocation that received the same per-trade $ flows on the same days. Rows sum to the total at the bottom. Switch to QQQ/Policy on the Dashboard's chart to compare different benchmarks.",
};

const V_START_EXPLAINER = {
  definition:
    "Aggregate dollar value of positions on the window-start date — qty × that day's close, summed across tickers. Cash and SGOV are excluded.",
  interpretation:
    "This is the starting capital the SPY/QQQ/Policy counterfactuals are anchored to. Re-baselined whenever you change the date range.",
};

const COUNTERFACTUAL_EXPLAINER = {
  definition:
    "What V_start would have grown to if invested in the benchmark on the start date, then dollar-matched every in-window buy and sell at that day's benchmark close.",
  interpretation:
    "If you bought $5,000 of NVDA on March 1, the SPY counterfactual gets $5,000-worth of SPY shares added on March 1. Sells subtract. This isolates your stock-picking skill from contribution timing.",
};

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
            <th className="px-3 py-2 text-right font-medium">
              <span className="inline-flex items-center justify-end gap-1">
                V start
                <InfoButton label="V start" explainer={V_START_EXPLAINER} />
              </span>
            </th>
            <th className="px-3 py-2 text-right font-medium">Bought</th>
            <th className="px-3 py-2 text-right font-medium">Sold</th>
            <th className="px-3 py-2 text-right font-medium">V end</th>
            <th className="px-3 py-2 text-right font-medium">Actual P&amp;L</th>
            <th className="px-3 py-2 text-right font-medium">
              <span className="inline-flex items-center justify-end gap-1">
                SPY counterfactual
                <InfoButton
                  label="SPY counterfactual"
                  explainer={COUNTERFACTUAL_EXPLAINER}
                />
              </span>
            </th>
            <th className="px-3 py-2 text-right font-medium">
              <span className="inline-flex items-center justify-end gap-1">
                Alpha
                <InfoButton
                  label="Position alpha"
                  explainer={POSITION_ALPHA_EXPLAINER}
                />
              </span>
            </th>
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
      <div className="border-t border-slate-100 bg-slate-50 px-3 py-2 text-[11px] leading-relaxed text-slate-500">
        <strong>SPY counterfactual</strong> = your V_start invested in SPY on the
        start date, plus dollar-matched buys/sells at each event's SPY close.
        <strong className="ml-1.5">Alpha</strong> = Actual P&amp;L − SPY P&amp;L.
        Positive ⇒ your position beat an equivalent SPY allocation with the
        same trade timing.{" "}
        <span className="text-amber-700">⚠</span> = missing price data;{" "}
        <span className="rounded bg-rose-100 px-1 text-[9px] font-semibold uppercase text-rose-800">UNREL</span>{" "}
        = broker cost basis is junk on at least one account (alpha is still
        valid; lifetime P&amp;L on the Trade Analysis page is the affected
        number).
      </div>
    </div>
  );
}
