import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import { PrimaryButton } from "@/components/ui";
import type { DataQualityFindingOut } from "@/types";

const SEVERITY_STYLES: Record<DataQualityFindingOut["severity"], string> = {
  info: "border-slate-200 bg-slate-50 text-slate-700",
  warning: "border-amber-200 bg-amber-50 text-amber-900",
  error: "border-red-200 bg-red-50 text-red-900",
};

const SEVERITY_BADGE: Record<DataQualityFindingOut["severity"], string> = {
  info: "bg-slate-200 text-slate-700",
  warning: "bg-amber-200 text-amber-900",
  error: "bg-red-200 text-red-900",
};

export function DataQualityReport(): JSX.Element {
  const [open, setOpen] = useState(false);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["data-quality"],
    queryFn: () => api.dataQuality(),
  });

  if (isLoading) {
    return <div className="text-xs text-slate-500">Checking data quality…</div>;
  }
  if (isError || !data) return <></>;

  const total = data.findings.length;
  if (total === 0) {
    return (
      <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
        ✓ No data-quality issues detected.
      </div>
    );
  }

  const counts = data.summary_counts;
  const summary = ["error", "warning", "info"]
    .filter((sev) => (counts[sev] ?? 0) > 0)
    .map((sev) => `${counts[sev]} ${sev}`)
    .join(" · ");

  return (
    <div className="rounded-md border border-slate-200 bg-white">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs hover:bg-slate-50"
      >
        <span className="flex items-center gap-2">
          <span className="text-slate-400">{open ? "▾" : "▸"}</span>
          <span className="font-medium text-slate-700">
            Data quality: {total} finding{total === 1 ? "" : "s"}
          </span>
          <span className="text-slate-500">({summary})</span>
        </span>
        <span className="text-slate-400">click to expand</span>
      </button>
      {open && (
        <ul className="divide-y divide-slate-100 border-t border-slate-100">
          {data.findings.map((f, idx) => (
            <FindingRow key={`${f.category}-${idx}`} finding={f} />
          ))}
        </ul>
      )}
    </div>
  );
}

function FindingRow({ finding }: { finding: DataQualityFindingOut }): JSX.Element {
  return (
    <li
      className={[
        "px-3 py-2 border-l-4",
        SEVERITY_STYLES[finding.severity],
      ].join(" ")}
    >
      <div className="flex items-center gap-2">
        <span
          className={[
            "text-xs font-medium px-1.5 py-0.5 rounded uppercase tracking-wide",
            SEVERITY_BADGE[finding.severity],
          ].join(" ")}
        >
          {finding.severity}
        </span>
        <span className="text-sm font-medium">{finding.title}</span>
      </div>
      <p className="mt-1 text-xs text-slate-600 max-w-prose">{finding.detail}</p>
      {finding.recommended_action && (
        <p className="mt-1 text-xs text-slate-700">
          <span className="font-medium">Action:</span>{" "}
          {finding.recommended_action}
        </p>
      )}
      <FindingOverride finding={finding} />
    </li>
  );
}

function FindingOverride({ finding }: { finding: DataQualityFindingOut }): JSX.Element {
  if (finding.category === "missing_cost_basis") {
    return <CostBasisOverrideForm finding={finding} />;
  }
  if (finding.category === "untickered_security") {
    return <TickerOverrideForm finding={finding} />;
  }
  return <></>;
}

function CostBasisOverrideForm({
  finding,
}: {
  finding: DataQualityFindingOut;
}): JSX.Element {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);

  const accountId = parseInt(finding.context.account_id ?? "0", 10);
  const securityId = parseInt(finding.context.security_id ?? "0", 10);
  const quantity = parseFloat(finding.context.quantity ?? "0");
  const currentPrice = parseFloat(finding.context.institution_price ?? "0");
  const ticker = finding.context.ticker ?? "?";

  const save = useMutation({
    mutationFn: (totalCost: number) =>
      api.setCostBasisOverride({
        account_id: accountId,
        security_id: securityId,
        total_cost_basis: totalCost,
        notes: notes.trim() || null,
      }),
    onSuccess: () => {
      setError(null);
      setValue("");
      setNotes("");
      queryClient.invalidateQueries({ queryKey: ["data-quality"] });
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Save failed"),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const total = parseFloat(value);
    if (Number.isNaN(total) || total < 0) {
      setError("Enter a non-negative dollar amount");
      return;
    }
    save.mutate(total);
  };

  const perShareHint =
    value && quantity > 0 && parseFloat(value) > 0
      ? `≈ $${(parseFloat(value) / quantity).toFixed(2)}/share`
      : "";

  return (
    <form
      onSubmit={onSubmit}
      className="mt-2 rounded border border-slate-200 bg-white p-2.5 space-y-2"
    >
      <div className="text-xs text-slate-700">
        <strong>How to find this:</strong> Open your brokerage statement (or the
        Transactions page in this app) and sum the total dollars paid across
        all purchases for {ticker} in this account, including commissions and
        fees. Subtract any partial sales that have already been realized.
        {currentPrice > 0 && (
          <>
            {" "}
            Quick estimate: {quantity.toFixed(2)} shares × current $
            {currentPrice.toFixed(2)} = $
            {(quantity * currentPrice).toLocaleString(undefined, {
              maximumFractionDigits: 0,
            })}{" "}
            — but use your actual purchase price, not today's.
          </>
        )}
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-xs text-slate-600">
          Total cost basis ($)
          <input
            type="number"
            step="0.01"
            min="0"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="e.g. 32500"
            className="mt-1 w-40 rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
          />
        </label>
        <label className="flex flex-col text-xs text-slate-600 flex-1 min-w-[160px]">
          Note (optional)
          <input
            type="text"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="e.g. ACATS from Vanguard 2023"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <PrimaryButton type="submit" disabled={save.isPending}>
          Save
        </PrimaryButton>
        {perShareHint && (
          <span className="text-xs text-slate-500 self-center">{perShareHint}</span>
        )}
      </div>
      {error && <div className="text-xs text-red-700">{error}</div>}
    </form>
  );
}

function TickerOverrideForm({
  finding,
}: {
  finding: DataQualityFindingOut;
}): JSX.Element {
  const queryClient = useQueryClient();
  const [ticker, setTicker] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);

  const securityId = parseInt(finding.context.security_id ?? "0", 10);
  const securityName = finding.context.security_name || "this security";

  const save = useMutation({
    mutationFn: (sym: string) =>
      api.setTickerOverride({
        security_id: securityId,
        ticker: sym,
        notes: notes.trim() || null,
      }),
    onSuccess: () => {
      setError(null);
      setTicker("");
      setNotes("");
      queryClient.invalidateQueries({ queryKey: ["data-quality"] });
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Save failed"),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const sym = ticker.trim().toUpperCase();
    if (!sym) {
      setError("Ticker is required");
      return;
    }
    save.mutate(sym);
  };

  return (
    <form
      onSubmit={onSubmit}
      className="mt-2 rounded border border-slate-200 bg-white p-2.5 space-y-2"
    >
      <div className="text-xs text-slate-700">
        <strong>How to find this:</strong> Search Yahoo Finance for{" "}
        {securityName}. The ticker shown there (e.g., <code>VTSAX</code>,{" "}
        <code>7203.T</code>, <code>BRK-B</code>) is what yfinance accepts.
        After saving, run{" "}
        <code className="bg-slate-100 px-1 rounded">
          python -m portfolio_tracker.jobs.prices --start 2024-01-01
        </code>{" "}
        from a terminal to populate historical prices.
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-xs text-slate-600">
          Ticker symbol
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            placeholder="VTSAX"
            className="mt-1 w-40 rounded border border-slate-300 px-2 py-1 text-sm uppercase tabular-nums"
          />
        </label>
        <label className="flex flex-col text-xs text-slate-600 flex-1 min-w-[160px]">
          Note (optional)
          <input
            type="text"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <PrimaryButton type="submit" disabled={save.isPending}>
          Save
        </PrimaryButton>
      </div>
      {error && <div className="text-xs text-red-700">{error}</div>}
    </form>
  );
}
