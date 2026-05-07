import type {
  BetaResult,
  CashflowAuditOut,
  ConsolidatedHoldingOut,
  DataQualityReportOut,
  ExchangePublicTokenOut,
  HoldingOut,
  InvestmentTransactionOut,
  ItemOut,
  LinkTokenOut,
  PerformanceSeries,
  PolicyOut,
  PolicyWeightIn,
} from "@/types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  createLinkToken: (profile: "primary" | "spouse" = "primary"): Promise<LinkTokenOut> =>
    request(`/api/plaid/link-token?profile=${profile}`, { method: "POST" }),

  exchangePublicToken: (input: {
    public_token: string;
    institution_id?: string | null;
    institution_name?: string | null;
  }): Promise<ExchangePublicTokenOut> =>
    request("/api/plaid/exchange-token", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  listItems: (): Promise<ItemOut[]> => request("/api/plaid/items"),

  unlinkItem: (itemId: number): Promise<void> =>
    request(`/api/plaid/items/${itemId}`, { method: "DELETE" }),

  latestHoldings: (): Promise<ConsolidatedHoldingOut[]> =>
    request("/api/portfolio/holdings"),

  latestHoldingsByAccount: (): Promise<HoldingOut[]> =>
    request("/api/portfolio/holdings/by-account"),

  cashflowAudit: (): Promise<CashflowAuditOut> =>
    request("/api/portfolio/cashflow-audit"),

  dataQuality: (): Promise<DataQualityReportOut> =>
    request("/api/portfolio/data-quality"),

  beta: (params?: {
    startDate?: string;
    endDate?: string;
    benchmark?: string;
    riskFreeAnnual?: number;
  }): Promise<BetaResult> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    if (params?.benchmark) search.set("benchmark", params.benchmark);
    if (params?.riskFreeAnnual !== undefined) {
      search.set("risk_free_annual", String(params.riskFreeAnnual));
    }
    const qs = search.toString();
    return request(`/api/portfolio/beta${qs ? `?${qs}` : ""}`);
  },

  getPolicy: (): Promise<PolicyOut> => request("/api/policy"),

  putPolicy: (weights: PolicyWeightIn[]): Promise<PolicyOut> =>
    request("/api/policy", { method: "PUT", body: JSON.stringify(weights) }),

  setCostBasisOverride: (input: {
    account_id: number;
    security_id: number;
    total_cost_basis: number;
    notes?: string | null;
  }): Promise<unknown> =>
    request("/api/overrides/cost-basis", {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  deleteCostBasisOverride: (
    accountId: number,
    securityId: number,
  ): Promise<void> =>
    request(`/api/overrides/cost-basis/${accountId}/${securityId}`, {
      method: "DELETE",
    }),

  setTickerOverride: (input: {
    security_id: number;
    ticker: string;
    notes?: string | null;
  }): Promise<unknown> =>
    request("/api/overrides/ticker", {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  deleteTickerOverride: (securityId: number): Promise<void> =>
    request(`/api/overrides/ticker/${securityId}`, { method: "DELETE" }),

  transactions: (params?: {
    startDate?: string;
    endDate?: string;
    limit?: number;
  }): Promise<InvestmentTransactionOut[]> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    if (params?.limit) search.set("limit", String(params.limit));
    const qs = search.toString();
    return request(`/api/portfolio/transactions${qs ? `?${qs}` : ""}`);
  },

  performance: (params?: {
    startDate?: string;
    endDate?: string;
    includeBackfill?: boolean;
  }): Promise<PerformanceSeries> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    if (params?.includeBackfill !== undefined) {
      search.set("include_backfill", String(params.includeBackfill));
    }
    const qs = search.toString();
    return request(`/api/portfolio/performance${qs ? `?${qs}` : ""}`);
  },

  // SnapTrade
  snaptradeStatus: (): Promise<{ configured: boolean }> =>
    request("/api/snaptrade/status"),

  snaptradePortalUrl: (
    profile: "primary" | "spouse" = "primary",
  ): Promise<{ url: string }> =>
    request(`/api/snaptrade/connection-portal-url?profile=${profile}`, { method: "POST" }),

  snaptradeSync: (
    profile: "primary" | "spouse" = "primary",
  ): Promise<{
    profile: string;
    items_synced: number;
    accounts_synced: number;
    holdings_written: number;
    transactions_written: number;
  }> => request(`/api/snaptrade/sync?profile=${profile}`, { method: "POST" }),
};
