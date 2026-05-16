import { TradeTimelineCard } from "@/components/TradeTimelineCard";

/**
 * Standalone page for the chronological Trade Timeline. The card itself
 * already owns its data fetch, year filter, sort modes, and pagination
 * — this page just hosts it under a stable route so it doesn't compete
 * for space on the Dashboard.
 */
export function TradeTimeline(): JSX.Element {
  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold text-slate-900">
          Trade timeline vs SPY
        </h1>
        <p className="mt-0.5 text-sm text-slate-500">
          Every closed 1099-B lot and open position scored against a
          buy-and-hold-SPY counterfactual over the same holding window.
        </p>
      </header>
      <TradeTimelineCard />
    </div>
  );
}
