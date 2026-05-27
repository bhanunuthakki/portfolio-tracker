import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import { Card, InfoButton } from "@/components/ui";
import type { CoachingTip } from "@/types";

/**
 * CIO coaching panel.
 *
 * Reads /api/coaching/tips, groups red-flag tips by category, and renders
 * them as a scannable list. Threshold values come from the backend rubric
 * (mirrors CIO_CONTEXT.md) so changing the file + restarting the API is
 * sufficient — no frontend redeploy needed.
 *
 * The rubric/persona prose itself isn't loaded into the UI — that's the
 * job of your LLM coaching chat. This panel is the deterministic
 * "what's off-rubric right now" layer that the LLM can cite.
 */
export function CoachingPanel(): JSX.Element {
  const [collapsed, setCollapsed] = useState(false);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["coaching-tips"],
    queryFn: () => api.coachingTips(),
  });

  const grouped = useMemo(() => {
    const buckets = new Map<CoachingTip["category"], CoachingTip[]>();
    for (const tip of data?.tips ?? []) {
      const arr = buckets.get(tip.category) ?? [];
      arr.push(tip);
      buckets.set(tip.category, arr);
    }
    return buckets;
  }, [data?.tips]);

  if (isLoading) {
    return (
      <Card className="px-4 py-3 text-xs text-slate-500">
        Loading coaching tips…
      </Card>
    );
  }
  if (isError || !data) {
    return (
      <Card className="px-4 py-3 text-xs text-rose-700">
        Failed to load coaching tips.
      </Card>
    );
  }

  const tipCount = data.tips.length;
  const highCount = data.tips.filter((t) => t.severity === "high").length;
  const warningCount = data.tips.filter((t) => t.severity === "warning").length;

  return (
    <Card>
      <header className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-900">
            CIO coaching
            <InfoButton
              label="Coaching rubric"
              explainer={{
                definition: data.rubric_summary,
                interpretation:
                  "Tips here are deterministic rules — they evaluate the rubric mechanically against your latest holdings + transaction log + decision log. For qualitative advice, see CIO_CONTEXT.md in the repo (also referenced by your coaching LLM chat).",
              }}
            />
          </h2>
          <div className="flex items-center gap-1.5 text-[11px]">
            {highCount > 0 && (
              <SeverityBadge severity="high" count={highCount} />
            )}
            {warningCount > 0 && (
              <SeverityBadge severity="warning" count={warningCount} />
            )}
            {tipCount === 0 && (
              <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-800">
                ✓ rubric clean
              </span>
            )}
          </div>
          <span className="text-[11px] text-slate-500">
            {data.positions_evaluated} active positions evaluated
          </span>
        </div>
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="self-start rounded border border-slate-200 px-2 py-0.5 text-[11px] font-medium text-slate-600 hover:bg-slate-100"
        >
          {collapsed ? "Expand" : "Collapse"}
        </button>
      </header>

      {!collapsed && (
        <div className="divide-y divide-slate-100">
          {tipCount === 0 ? (
            <p className="px-4 py-4 text-xs text-slate-500">
              No red flags against the rubric right now. Coaching tips fire on
              IRR &lt; {data.rubric.irr_floor_pct}% over{" "}
              {data.rubric.min_hold_years_for_irr_check}+ year holds,
              single-name weight &gt; {data.rubric.concentration_max_pct}%,
              theses &gt; {data.rubric.thesis_stale_days}d stale,{" "}
              {data.rubric.multiples_detachment_factor}× detachment without a
              trim decision, or drawdowns &gt; {data.rubric.drawdown_audit_pct}
              % without a thesis audit in{" "}
              {data.rubric.drawdown_audit_window_days}d.
            </p>
          ) : (
            CATEGORY_ORDER.filter((c) => grouped.has(c)).map((category) => (
              <CategorySection
                key={category}
                category={category}
                tips={grouped.get(category)!}
              />
            ))
          )}
        </div>
      )}
    </Card>
  );
}

const CATEGORY_ORDER: CoachingTip["category"][] = [
  "drawdown_audit",
  "concentration_human_capital_aggregate",
  "concentration_human_capital",
  "irr_below_bar",
  "multiples_detachment",
  "concentration_limit",
  "thesis_stale",
];

const CATEGORY_LABELS: Record<CoachingTip["category"], string> = {
  drawdown_audit: "Drawdown requires audit",
  concentration_human_capital_aggregate:
    "Aggregate human-capital bucket over cap",
  concentration_human_capital: "Concentration vs. human capital",
  irr_below_bar: "IRR below the 10–12 % bar",
  multiples_detachment: "Multiples have detached — trim candidates",
  concentration_limit: "Single-name concentration over cap",
  thesis_stale: "Thesis stale or missing",
};

const CATEGORY_DESCRIPTIONS: Record<CoachingTip["category"], string> = {
  drawdown_audit:
    "Strategic Directive 3: a macro drawdown is a trigger for a micro-economic audit, not an automatic hold. Liquidate if the drawdown coincides with a broken thesis.",
  concentration_human_capital_aggregate:
    "Portfolio-wide weighted exposure (Σ position × bucket weight) to one human-capital bucket exceeds the bucket-level cap. Catches the 'twenty small ad-tech positions' case the single-name check misses. Edit bucket weights on the Accounts page; edit caps in services/coaching.py.",
  concentration_human_capital:
    "Single position over the per-name cap AND carrying weight in a human-capital bucket. Stacks portfolio risk on top of salary risk.",
  irr_below_bar:
    "Non-index positions need to clear ~10–12 %/yr to justify the tax + concentration cost over SPY. Below that, re-underwrite or rotate.",
  multiples_detachment:
    "When a satellite 2×s without thesis confirmation, fund Dry Powder (SGOV) by trimming. Re-deploy on the next macro panic.",
  concentration_limit:
    "Concentration target is 5–10 names with no single name materially above 8 %. Either log explicit high-conviction reasoning or trim.",
  thesis_stale:
    "Without a written thesis (or with one > 180 days old), invalidation triggers can't be audited objectively when the position moves against you.",
};

function CategorySection({
  category,
  tips,
}: {
  category: CoachingTip["category"];
  tips: CoachingTip[];
}): JSX.Element {
  return (
    <section className="px-4 py-3">
      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
        {CATEGORY_LABELS[category]}{" "}
        <span className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 normal-case tracking-normal">
          {tips.length}
        </span>
      </h3>
      <p className="mt-0.5 text-[11px] text-slate-500">
        {CATEGORY_DESCRIPTIONS[category]}
      </p>
      <ul className="mt-2 space-y-2">
        {tips.map((tip, idx) => (
          <TipRow key={`${tip.ticker}-${idx}`} tip={tip} />
        ))}
      </ul>
    </section>
  );
}

function TipRow({ tip }: { tip: CoachingTip }): JSX.Element {
  return (
    <li className="rounded-md border border-slate-100 bg-slate-50/60 px-3 py-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-500">
            <SeverityChip severity={tip.severity} />
            <span className="font-mono font-medium text-slate-800">
              {tip.ticker}
            </span>
            {tip.name && (
              <span className="truncate text-slate-500">{tip.name}</span>
            )}
          </div>
          <p className="mt-1 text-sm font-medium text-slate-900 leading-snug">
            {tip.headline}
          </p>
          <p className="mt-1 text-xs text-slate-600 leading-relaxed">
            {tip.detail}
          </p>
          <p className="mt-1.5 text-xs text-slate-800">
            <span className="font-semibold">Action: </span>
            {tip.suggested_action}
          </p>
        </div>
      </div>
    </li>
  );
}

function SeverityBadge({
  severity,
  count,
}: {
  severity: "high" | "warning";
  count: number;
}): JSX.Element {
  const cls =
    severity === "high"
      ? "bg-rose-100 text-rose-800"
      : "bg-amber-100 text-amber-800";
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${cls}`}
    >
      {count} {severity}
    </span>
  );
}

function SeverityChip({
  severity,
}: {
  severity: CoachingTip["severity"];
}): JSX.Element {
  const cls =
    severity === "high"
      ? "bg-rose-100 text-rose-800"
      : severity === "warning"
        ? "bg-amber-100 text-amber-800"
        : "bg-slate-200 text-slate-700";
  return (
    <span
      className={`rounded px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${cls}`}
    >
      {severity}
    </span>
  );
}
