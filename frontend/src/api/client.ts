import type {
  ConsolidatedHoldingOut,
  DataQualityReportOut,
  ExchangePublicTokenOut,
  InvestmentTransactionOut,
  ItemOut,
  LinkTokenOut,
  PolicyOut,
  PolicyWeightIn,
  PositioningOut,
  SecurityClassificationOut,
  TransactionOverrideIn,
  TransactionOverrideOut,
  CashflowRuleAuditOut,
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

  setItemDataActive: (itemId: number, isActive: boolean): Promise<ItemOut> =>
    request(`/api/plaid/items/${itemId}/data-active`, {
      method: "PATCH",
      body: JSON.stringify({ is_data_active: isActive }),
    }),

  latestHoldings: (): Promise<ConsolidatedHoldingOut[]> =>
    request("/api/portfolio/holdings"),

  positioning: (params?: {
    startDate?: string;
    endDate?: string;
  }): Promise<PositioningOut> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    const qs = search.toString();
    return request(`/api/portfolio/positioning${qs ? `?${qs}` : ""}`);
  },

  setSecurityClassification: (input: {
    security_id: number;
    sector?: string | null;
    region?: string | null;
    notes?: string | null;
  }): Promise<SecurityClassificationOut> =>
    request("/api/overrides/security-classification", {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  deleteSecurityClassification: (securityId: number): Promise<void> =>
    request(`/api/overrides/security-classification/${securityId}`, {
      method: "DELETE",
    }),

  dataQuality: (): Promise<DataQualityReportOut> =>
    request("/api/portfolio/data-quality"),

  getPolicy: (): Promise<PolicyOut> => request("/api/policy"),

  putPolicy: (weights: PolicyWeightIn[]): Promise<PolicyOut> =>
    request("/api/policy", { method: "PUT", body: JSON.stringify(weights) }),

  setCostBasisOverride: (input: {
    account_id: number;
    security_id: number;
    total_cost_basis: number;
    notes?: string | null;
    acquired_at?: string | null;
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

  setTransactionOverride: (
    input: TransactionOverrideIn,
  ): Promise<TransactionOverrideOut> =>
    request("/api/overrides/transactions", {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  deleteTransactionOverride: (
    plaidInvestmentTransactionId: string,
  ): Promise<void> =>
    request(
      `/api/overrides/transactions/${encodeURIComponent(plaidInvestmentTransactionId)}`,
      { method: "DELETE" },
    ),

  cashflowRuleAudit: (): Promise<CashflowRuleAuditOut> =>
    request("/api/portfolio/cashflow-audit-rules"),

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
