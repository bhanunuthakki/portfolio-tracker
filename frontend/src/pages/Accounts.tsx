import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { usePlaidLink } from "react-plaid-link";

import { api } from "@/api/client";
import { PolicyEditor } from "@/components/PolicyEditor";
import { SnapTradeButtons } from "@/components/SnapTradeButtons";
import {
  Card,
  DangerLink,
  EmptyState,
  ErrorBanner,
  PageHeader,
  PrimaryButton,
  SecondaryButton,
} from "@/components/ui";

export function Accounts(): JSX.Element {
  const queryClient = useQueryClient();
  const items = useQuery({ queryKey: ["items"], queryFn: () => api.listItems() });

  const [linkToken, setLinkToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchLinkToken = useCallback(async (profile: "primary" | "spouse") => {
    setError(null);
    try {
      const { link_token } = await api.createLinkToken(profile);
      setLinkToken(link_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch link token");
    }
  }, []);

  const exchange = useMutation({
    mutationFn: (input: {
      publicToken: string;
      institutionId: string | null;
      institutionName: string | null;
    }) =>
      api.exchangePublicToken({
        public_token: input.publicToken,
        institution_id: input.institutionId,
        institution_name: input.institutionName,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["items"] });
      setLinkToken(null);
    },
    onError: (err) =>
      setError(err instanceof Error ? err.message : "Exchange failed"),
  });

  const unlink = useMutation({
    mutationFn: (id: number) => api.unlinkItem(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["items"] }),
  });

  const setDataActive = useMutation({
    mutationFn: ({ itemId, isActive }: { itemId: number; isActive: boolean }) =>
      api.setItemDataActive(itemId, isActive),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["items"] });
      // Aggregations elsewhere in the app depend on this; bust their caches.
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["data-quality"] });
      queryClient.invalidateQueries({ queryKey: ["positioning"] });
    },
  });

  const { open, ready } = usePlaidLink({
    token: linkToken,
    onSuccess: (publicToken, metadata) =>
      exchange.mutate({
        publicToken,
        institutionId: metadata.institution?.institution_id ?? null,
        institutionName: metadata.institution?.name ?? null,
      }),
    onExit: (err) => {
      if (err) {
        setError(err.display_message ?? err.error_message ?? "Plaid Link exited");
      }
      setLinkToken(null);
    },
  });

  useEffect(() => {
    if (linkToken && ready) {
      open();
    }
  }, [linkToken, ready, open]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Linked institutions"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <SourceLabel>via Plaid:</SourceLabel>
            <PrimaryButton onClick={() => fetchLinkToken("primary")}>
              + Mine
            </PrimaryButton>
            <SecondaryButton onClick={() => fetchLinkToken("spouse")}>
              + Spouse&apos;s
            </SecondaryButton>
            <SourceLabel className="ml-1">via SnapTrade:</SourceLabel>
            <SnapTradeButtons
              onSyncComplete={() =>
                queryClient.invalidateQueries({ queryKey: ["items"] })
              }
            />
          </div>
        }
      />

      {error && <ErrorBanner>{error}</ErrorBanner>}

      {items.isLoading ? (
        <div className="text-sm text-slate-500">Loading…</div>
      ) : items.data && items.data.length > 0 ? (
        <ul className="space-y-3">
          {items.data.map((item) => (
            <Card
              key={item.item_id}
              className={`p-4 ${item.is_data_active ? "" : "bg-slate-50/60"}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-slate-900 truncate">
                      {item.institution_name ?? "Unknown institution"}
                    </span>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-slate-600">
                      {item.source}
                    </span>
                    {!item.is_data_active && (
                      <span
                        className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-amber-800"
                        title="Connection stays linked, but its data is excluded from every aggregation."
                      >
                        data inactive
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-slate-500">
                    Linked {new Date(item.linked_at).toLocaleDateString()} ·{" "}
                    {item.accounts.length}{" "}
                    {item.accounts.length === 1 ? "account" : "accounts"}
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() =>
                      setDataActive.mutate({
                        itemId: item.item_id,
                        isActive: !item.is_data_active,
                      })
                    }
                    disabled={setDataActive.isPending}
                    className="text-xs font-medium text-slate-600 underline-offset-2 hover:text-slate-900 hover:underline"
                    title={
                      item.is_data_active
                        ? "Stop counting this Item's data in aggregations (keeps the connection alive)."
                        : "Start counting this Item's data in aggregations again."
                    }
                  >
                    {item.is_data_active
                      ? "Exclude from data"
                      : "Re-include in data"}
                  </button>
                  <DangerLink onClick={() => unlink.mutate(item.item_id)}>
                    Unlink
                  </DangerLink>
                </div>
              </div>
              <ul className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                {item.accounts.map((account) => (
                  <li
                    key={account.account_id}
                    className="rounded border border-slate-100 bg-slate-50 px-3 py-2 text-sm"
                  >
                    <div className="font-medium text-slate-900 truncate">
                      {account.name}
                    </div>
                    <div className="text-xs text-slate-500">
                      {account.subtype ?? account.type}
                      {account.mask && ` · ····${account.mask}`}
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          ))}
        </ul>
      ) : (
        <EmptyState>
          No accounts linked yet. Use <strong>+ Mine</strong> or{" "}
          <strong>+ Spouse&apos;s</strong> above to start.
        </EmptyState>
      )}

      <PolicyEditor />
    </div>
  );
}

function SourceLabel({
  children,
  className,
}: {
  children: string;
  className?: string;
}): JSX.Element {
  return (
    <span
      className={[
        "text-[11px] uppercase tracking-wider text-slate-500",
        className ?? "",
      ].join(" ")}
    >
      {children}
    </span>
  );
}
