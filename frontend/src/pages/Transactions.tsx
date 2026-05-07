import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { Card, EmptyState, ErrorBanner, Td, Th } from "@/components/ui";

export function Transactions(): JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["transactions"],
    queryFn: () => api.transactions({ limit: 500 }),
  });

  if (isLoading) return <div className="text-sm text-slate-500">Loading…</div>;
  if (isError)
    return <ErrorBanner>Failed to load transactions.</ErrorBanner>;
  if (!data || data.length === 0) {
    return (
      <EmptyState>
        No transactions yet. Link an account and run the backfill job.
      </EmptyState>
    );
  }

  return (
    <Card className="overflow-hidden">
      <table className="min-w-full divide-y divide-slate-200">
        <thead className="bg-slate-50">
          <tr>
            <Th>Date</Th>
            <Th>Account</Th>
            <Th>Type</Th>
            <Th>Ticker</Th>
            <Th>Description</Th>
            <Th align="right">Quantity</Th>
            <Th align="right">Amount</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {data.map((t) => (
            <tr key={t.plaid_investment_transaction_id} className="hover:bg-slate-50">
              <Td className="tabular-nums text-slate-900">{t.date}</Td>
              <Td>{t.account_name}</Td>
              <Td>
                <span className="inline-flex rounded-full bg-slate-100 px-2 py-0.5 text-xs uppercase tracking-wide text-slate-700">
                  {t.subtype ?? t.type}
                </span>
              </Td>
              <Td className="font-mono text-slate-900">{t.ticker ?? "—"}</Td>
              <Td className="text-slate-600">{t.name ?? "—"}</Td>
              <Td align="right" className="tabular-nums">
                {parseFloat(t.quantity).toLocaleString(undefined, {
                  maximumFractionDigits: 4,
                })}
              </Td>
              <Td align="right" className="tabular-nums">
                $
                {parseFloat(t.amount).toLocaleString(undefined, {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="border-t border-slate-100 bg-slate-50 px-4 py-2.5 text-xs text-slate-500">
        Showing the most recent {data.length} transactions across all linked
        accounts. Type tags follow Plaid&apos;s subtype taxonomy.
      </p>
    </Card>
  );
}
