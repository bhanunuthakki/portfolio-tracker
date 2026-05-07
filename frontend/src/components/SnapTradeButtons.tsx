import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import { ErrorBanner, InfoBanner, SecondaryButton } from "@/components/ui";

type Profile = "primary" | "spouse";

export function SnapTradeButtons({
  onSyncComplete,
}: {
  onSyncComplete: () => void;
}): JSX.Element {
  const status = useQuery({
    queryKey: ["snaptrade-status"],
    queryFn: () => api.snaptradeStatus(),
  });
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const openPortal = useMutation({
    mutationFn: (profile: Profile) => api.snaptradePortalUrl(profile),
    onSuccess: (data, profile) => {
      window.open(data.url, "_blank", "noopener,noreferrer");
      setInfo(
        `Portal opened for ${profile} in a new tab. After you finish linking your brokerage, click "Sync ${profile}" below.`,
      );
      setError(null);
    },
    onError: (err) =>
      setError(err instanceof Error ? err.message : "Portal failed"),
  });

  const sync = useMutation({
    mutationFn: (profile: Profile) => api.snaptradeSync(profile),
    onSuccess: (data) => {
      setInfo(
        `Synced: ${data.items_synced} brokerage(s), ${data.accounts_synced} account(s), ${data.holdings_written} holding(s), ${data.transactions_written} new transaction(s).`,
      );
      setError(null);
      onSyncComplete();
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Sync failed"),
  });

  if (status.isLoading) {
    return <span className="text-xs text-slate-500">…</span>;
  }
  if (!status.data?.configured) {
    return (
      <span className="text-xs text-slate-500">
        not configured (set SNAPTRADE_* in .env)
      </span>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <SnapButton onClick={() => openPortal.mutate("primary")}>
          + Mine
        </SnapButton>
        <SnapButton onClick={() => openPortal.mutate("spouse")}>
          + Spouse&apos;s
        </SnapButton>
        <SecondaryButton
          onClick={() => sync.mutate("primary")}
          disabled={sync.isPending}
        >
          Sync mine
        </SecondaryButton>
        <SecondaryButton
          onClick={() => sync.mutate("spouse")}
          disabled={sync.isPending}
        >
          Sync spouse&apos;s
        </SecondaryButton>
      </div>
      {info && <InfoBanner>{info}</InfoBanner>}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

/** SnapTrade-flavored button — same shape as PrimaryButton but blue, to
 * visually distinguish from Plaid actions in the same row. */
function SnapButton({
  children,
  onClick,
}: {
  children: React.ReactNode;
  onClick?: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-md border border-blue-300 bg-blue-50 px-3 py-1.5 text-sm font-medium text-blue-800 hover:bg-blue-100"
    >
      {children}
    </button>
  );
}
