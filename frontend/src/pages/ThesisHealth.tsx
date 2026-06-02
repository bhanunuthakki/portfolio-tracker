import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Panel,
  Pill,
  SortableTh,
  Td,
} from "@/components/ui";
import { useTableSort } from "@/components/useTableSort";
import type { ThesisHealthRow } from "@/types";

/**
 * Thesis Health — per-holding fundamental health, dollar-weighted.
 *
 * Reads /api/cockpit/thesis-health (sourced from the Earnings Summary project):
 * the thesis verdict, valuation gap, pending-alert count, and brief link for
 * each holding, plus blind-spot flagging for holdings with no coverage. Default
 * order floats breaches and blind spots to the top; every column is sortable.
 */

const VERDICT_TONE = { breach: "loss", warn: "warn", ok: "gain" } as const;
const VAL_TONE = { rich: "warn", cheap: "gain", fair: "neutral", unknown: "muted" } as const;

function attentionRank(r: ThesisHealthRow): number {
  if (r.verdict_status === "breach") return 0;
  if (r.verdict_status === "warn") return 1;
  if (!r.tracked || r.alert_count > 0) return 2;
  if (r.valuation_signal === "rich") return 3;
  return 4;
}

export function ThesisHealth(): JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["thesis-health"],
    queryFn: () => api.thesisHealth(),
  });
  const rows = data ?? [];
  const sort = useTableSort<ThesisHealthRow>(rows, "attention", "asc", {
    attention: (r) => attentionRank(r),
    ticker: (r) => r.ticker,
    weight: (r) => r.weight_pct,
    verdict: (r) => attentionRank(r),
    valuation: (r) => r.over_under_pct,
    alerts: (r) => r.alert_count,
  });

  return (
    <div className="space-y-5">
      <PageHeader title="Thesis health" eyebrow="Fundamentals" />
      {isLoading ? (
        <Card className="px-4 py-3 text-sm text-muted">Loading thesis health…</Card>
      ) : isError ? (
        <ErrorBanner>Failed to load thesis health.</ErrorBanner>
      ) : rows.length === 0 ? (
        <EmptyState>
          No holdings to assess — thesis health is sourced from the Earnings Summary project.
        </EmptyState>
      ) : (
        <Panel
          eyebrow={`${rows.length} holdings`}
          title="Where each holding's thesis & valuation stands"
          sub="Breaches and blind spots float to the top; weighted by your dollar exposure."
          bodyClassName="overflow-x-auto"
        >
          <table className="min-w-full divide-y divide-hairline">
            <thead>
              <tr>
                <SortableTh column="ticker" sort={sort}>
                  Holding
                </SortableTh>
                <SortableTh column="weight" align="right" sort={sort}>
                  Weight
                </SortableTh>
                <SortableTh column="verdict" sort={sort}>
                  Thesis
                </SortableTh>
                <SortableTh column="valuation" sort={sort}>
                  Valuation
                </SortableTh>
                <SortableTh column="alerts" align="right" sort={sort}>
                  Alerts
                </SortableTh>
              </tr>
            </thead>
            <tbody className="divide-y divide-hairline">
              {sort.sortedRows.map((r) => (
                <ThesisRow key={r.ticker} row={r} />
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}

function ThesisRow({ row }: { row: ThesisHealthRow }): JSX.Element {
  return (
    <tr className="align-top">
      <Td>
        <div className="font-mono text-sm font-semibold text-ink">{row.ticker}</div>
        {row.name && <div className="text-xs text-muted">{row.name}</div>}
        {row.has_brief && row.brief_iso_date && (
          <a
            href={`/api/earnings-summary/brief/${encodeURIComponent(row.ticker)}`}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-accent hover:underline"
          >
            brief · {row.brief_iso_date}
          </a>
        )}
      </Td>
      <Td align="right" className="font-mono tabular-nums">
        {row.weight_pct.toFixed(1)}%
      </Td>
      <Td>
        {row.verdict_status ? (
          <div className="space-y-1">
            <Pill tone={VERDICT_TONE[row.verdict_status]}>{row.verdict_status}</Pill>
            {row.flagged_rules.length > 0 && (
              <div className="text-xs leading-snug text-muted">
                {row.flagged_rules.join("; ")}
              </div>
            )}
          </div>
        ) : !row.tracked ? (
          <Pill tone="muted" title="Held but not covered in Earnings Summary">
            no coverage
          </Pill>
        ) : (
          <span className="text-xs text-faint">—</span>
        )}
      </Td>
      <Td>
        {row.valuation_signal && row.valuation_signal !== "unknown" ? (
          <div className="flex items-center gap-2">
            <Pill tone={VAL_TONE[row.valuation_signal]}>{row.valuation_signal}</Pill>
            {row.over_under_pct != null && (
              <span className="font-mono text-xs tabular-nums text-muted">
                {row.over_under_pct >= 0 ? "+" : ""}
                {(row.over_under_pct * 100).toFixed(0)}% vs fair
              </span>
            )}
          </div>
        ) : (
          <span className="text-xs text-faint">—</span>
        )}
      </Td>
      <Td align="right">
        {row.alert_count > 0 ? (
          <Pill tone="warn">{row.alert_count}</Pill>
        ) : (
          <span className="text-xs text-faint">—</span>
        )}
      </Td>
    </tr>
  );
}
