import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";

import { api } from "@/api/client";
import {
  Card,
  ErrorBanner,
  fmtUSD,
  InfoButton,
  MethodologyNote,
  SortableTh,
  Stat,
  Td,
} from "@/components/ui";
import { useTableSort } from "@/components/useTableSort";
import type {
  PositionCorrelationRow,
  PositioningBucketOut,
} from "@/types";

// Stable bar colors by slice index — the largest slice is always slate-700.
const BAR_COLORS = [
  "bg-slate-700",
  "bg-sky-600",
  "bg-emerald-600",
  "bg-amber-500",
  "bg-violet-600",
  "bg-rose-500",
  "bg-teal-600",
  "bg-slate-400",
];

function num(value: string | null): number | null {
  return value === null ? null : parseFloat(value);
}

function fmtPct(value: string | null): string {
  const n = num(value);
  return n === null ? "—" : `${n.toFixed(0)}%`;
}

function fmtRatio(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

function BreakdownPanel({
  title,
  buckets,
}: {
  title: string;
  buckets: PositioningBucketOut[];
}): JSX.Element {
  return (
    <Card className="p-4">
      <div className="mb-2.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
        {title}
      </div>
      {buckets.length === 0 ? (
        <div className="text-xs text-slate-400">No data.</div>
      ) : (
        <ul className="space-y-2">
          {buckets.map((b, i) => {
            const weight = parseFloat(b.weight_pct);
            return (
              <li key={b.label} className="text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-slate-700" title={b.label}>
                    {b.label}
                  </span>
                  <span className="shrink-0 tabular-nums text-slate-500">
                    {weight.toFixed(0)}% · {fmtUSD(parseFloat(b.value))}
                  </span>
                </div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
                  <div
                    aria-hidden="true"
                    className={`h-full rounded-full ${BAR_COLORS[i % BAR_COLORS.length]}`}
                    style={{ width: `${Math.min(Math.max(weight, 1), 100)}%` }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function ClassificationEditor({
  row,
  onDone,
}: {
  row: PositionCorrelationRow;
  onDone: () => void;
}): JSX.Element {
  const queryClient = useQueryClient();
  const [sector, setSector] = useState("");
  const [region, setRegion] = useState("");
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["positioning"] });
    queryClient.invalidateQueries({ queryKey: ["holdings"] });
  };

  const save = useMutation({
    mutationFn: () =>
      api.setSecurityClassification({
        security_id: row.security_id,
        sector: sector.trim() || null,
        region: region.trim() || null,
      }),
    onSuccess: () => {
      setError(null);
      invalidate();
      onDone();
    },
    onError: (e) => setError(e instanceof Error ? e.message : "Save failed"),
  });

  const clear = useMutation({
    mutationFn: () => api.deleteSecurityClassification(row.security_id),
    onSuccess: () => {
      setError(null);
      invalidate();
      onDone();
    },
    onError: (e) => setError(e instanceof Error ? e.message : "Clear failed"),
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      className="flex flex-wrap items-end gap-2 rounded border border-slate-200 bg-white p-3"
    >
      <div className="text-xs text-slate-700">
        Reclassify <strong>{row.ticker ?? row.name ?? "security"}</strong>
      </div>
      <label className="flex flex-col text-xs text-slate-600">
        Sector
        <input
          type="text"
          value={sector}
          onChange={(e) => setSector(e.target.value)}
          placeholder="e.g. Technology"
          autoFocus
          className="mt-1 w-40 rounded border border-slate-300 px-2 py-1 text-sm"
        />
      </label>
      <label className="flex flex-col text-xs text-slate-600">
        Region
        <select
          value={region}
          onChange={(e) => setRegion(e.target.value)}
          className="mt-1 w-36 rounded border border-slate-300 px-2 py-1 text-sm"
        >
          <option value="">— unset —</option>
          <option value="US">US</option>
          <option value="International">International</option>
        </select>
      </label>
      <button
        type="submit"
        disabled={save.isPending}
        className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50"
      >
        {save.isPending ? "Saving…" : "Save (manual)"}
      </button>
      <button
        type="button"
        onClick={() => clear.mutate()}
        disabled={clear.isPending}
        className="rounded border border-red-200 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
        title="Remove any manual classification and fall back to auto enrichment"
      >
        Reset to auto
      </button>
      {error && <div className="w-full text-xs text-red-700">{error}</div>}
    </form>
  );
}

function CorrelationTable({
  rows,
  hasPolicy,
}: {
  rows: PositionCorrelationRow[];
  hasPolicy: boolean;
}): JSX.Element {
  const [editing, setEditing] = useState<number | null>(null);
  const colSpan = hasPolicy ? 9 : 7;
  const sort = useTableSort<PositionCorrelationRow>(rows, "weight", "desc", {
    ticker: (r) => r.ticker ?? "",
    weight: (r) => parseFloat(r.weight_pct),
    corr_spy: (r) => r.correlation_spy,
    beta_spy: (r) => r.beta_spy,
    corr_qqq: (r) => r.correlation_qqq,
    beta_qqq: (r) => r.beta_qqq,
    corr_policy: (r) => r.correlation_policy,
    beta_policy: (r) => r.beta_policy,
  });

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-1.5 border-b border-slate-100 px-4 py-2.5">
        <span className="text-[11px] font-medium uppercase tracking-wider text-slate-500">
          Correlation &amp; beta to benchmarks
        </span>
        <InfoButton
          label="Correlation (ρ) & beta (β)"
          explainer={{
            definition:
              "Per-ticker correlation and beta of daily returns versus each benchmark over the window (trailing year by default). Cash is excluded.",
            interpretation:
              "ρ near 1 means the name moves with the index (little diversification benefit); a lower ρ adds diversification. β is the slope — how much the name moves per 1% index move.",
          }}
        />
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200">
          <thead className="bg-slate-50">
            <tr>
              <SortableTh column="ticker" sort={sort} pad="tight">
                Ticker
              </SortableTh>
              <SortableTh column="weight" align="right" sort={sort} pad="tight">
                Weight
              </SortableTh>
              <SortableTh column="corr_spy" align="right" sort={sort} pad="tight">
                ρ SPY
              </SortableTh>
              <SortableTh column="beta_spy" align="right" sort={sort} pad="tight">
                β SPY
              </SortableTh>
              <SortableTh column="corr_qqq" align="right" sort={sort} pad="tight">
                ρ QQQ
              </SortableTh>
              <SortableTh column="beta_qqq" align="right" sort={sort} pad="tight">
                β QQQ
              </SortableTh>
              {hasPolicy && (
                <>
                  <SortableTh column="corr_policy" align="right" sort={sort} pad="tight">
                    ρ Policy
                  </SortableTh>
                  <SortableTh column="beta_policy" align="right" sort={sort} pad="tight">
                    β Policy
                  </SortableTh>
                </>
              )}
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {sort.sortedRows.map((r) => {
              const open = editing === r.security_id;
              return (
                <Fragment key={r.security_id}>
                  <tr>
                    <Td className="font-mono text-slate-900">{r.ticker ?? "—"}</Td>
                    <Td align="right" className="tabular-nums">
                      {fmtPct(r.weight_pct)}
                    </Td>
                    <Td align="right" className="tabular-nums">
                      {r.sample_size < 2 ? (
                        <span
                          className="text-slate-400"
                          title="Not enough price history in the window"
                        >
                          n/a
                        </span>
                      ) : (
                        fmtRatio(r.correlation_spy)
                      )}
                    </Td>
                    <Td align="right" className="tabular-nums">
                      {fmtRatio(r.beta_spy)}
                    </Td>
                    <Td align="right" className="tabular-nums">
                      {fmtRatio(r.correlation_qqq)}
                    </Td>
                    <Td align="right" className="tabular-nums">
                      {fmtRatio(r.beta_qqq)}
                    </Td>
                    {hasPolicy && (
                      <>
                        <Td align="right" className="tabular-nums">
                          {fmtRatio(r.correlation_policy)}
                        </Td>
                        <Td align="right" className="tabular-nums">
                          {fmtRatio(r.beta_policy)}
                        </Td>
                      </>
                    )}
                    <Td align="right">
                      <button
                        type="button"
                        onClick={() => setEditing(open ? null : r.security_id)}
                        className="rounded px-1.5 py-0.5 text-[10px] font-medium text-slate-500 hover:bg-slate-200"
                        title="Set the sector / region classification for this security"
                      >
                        {open ? "Cancel" : "Classify"}
                      </button>
                    </Td>
                  </tr>
                  {open && (
                    <tr className="bg-slate-50/60">
                      <td colSpan={colSpan} className="px-3 py-2">
                        <ClassificationEditor row={r} onDone={() => setEditing(null)} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function PositioningSection(): JSX.Element | null {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["positioning"],
    queryFn: () => api.positioning(),
  });

  if (isLoading) {
    return <Card className="p-4 text-sm text-slate-500">Loading positioning…</Card>;
  }
  if (isError) {
    return <ErrorBanner>Failed to load positioning.</ErrorBanner>;
  }
  if (!data || parseFloat(data.total_value) <= 0) {
    return null;
  }

  const c = data.concentration;

  return (
    <section className="space-y-3" aria-label="Portfolio positioning">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold text-slate-900">Positioning</h2>
        <InfoButton
          label="Positioning"
          explainer={{
            definition:
              "How the current book splits across asset type, sector, region, and account tax-treatment — plus concentration and correlation. Value-weighted over the latest snapshot.",
            interpretation:
              "Sector/region come from yfinance and can be corrected per security via 'Classify'. Funds show as their own ETF/Fund sector bucket.",
          }}
        />
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label="Effective holdings"
          value={c.effective_holdings === null ? "—" : c.effective_holdings.toFixed(1)}
        />
        <Stat label="Top-5 weight" value={fmtPct(c.top5_weight_pct)} />
        <Stat label="Positions" value={String(c.num_positions)} />
        <Stat label="Avg ρ to SPY" value={fmtRatio(data.weighted_avg_correlation_spy)} />
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <BreakdownPanel title="Asset type" buckets={data.by_asset_type} />
        <BreakdownPanel title="Sector" buckets={data.by_sector} />
        <BreakdownPanel title="Region" buckets={data.by_region} />
        <BreakdownPanel title="Account type" buckets={data.by_account_type} />
      </div>

      <CorrelationTable rows={data.correlations} hasPolicy={data.has_policy} />

      {data.notes.length > 0 && (
        <MethodologyNote title="Notes & methodology">
          <ul className="list-disc space-y-1 pl-4">
            {data.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </MethodologyNote>
      )}
    </section>
  );
}
