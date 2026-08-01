export interface AccountOut {
  account_id: number;
  name: string;
  official_name: string | null;
  type: string;
  subtype: string | null;
  mask: string | null;
  currency: string;
}

export interface ItemOut {
  item_id: number;
  source: string;
  institution_name: string | null;
  plaid_institution_id: string | null;
  linked_at: string;
  last_refreshed_at: string | null;
  is_data_active: boolean;
  accounts: AccountOut[];
}

export type CostBasisSource = "manual" | "inferred_acats" | "inferred_1099";

export interface HoldingByAccountOut {
  account_id: number;
  account_name: string;
  quantity: string;
  institution_value: string | null;
  cost_basis: string | null;
  cost_basis_source: CostBasisSource | null;
  cost_basis_unreliable: boolean;
}

export interface ConsolidatedHoldingOut {
  snapshot_date: string;
  security_id: number;
  ticker: string | null;
  name: string | null;
  total_quantity: string;
  total_value: string | null;
  total_cost_basis: string | null;
  weighted_avg_cost_per_share: string | null;
  unrealized_pnl: string | null;
  accounts: HoldingByAccountOut[];
  currency: string;
  has_unreliable_cost_basis: boolean;
}

export interface DataQualityFindingOut {
  category: string;
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
  recommended_action: string | null;
  context: Record<string, string>;
}

export interface DataQualityReportOut {
  generated_at: string;
  findings: DataQualityFindingOut[];
  summary_counts: Record<string, number>;
}

export interface PolicyWeightOut {
  ticker: string;
  weight_pct: string;
  notes: string | null;
  updated_at: string;
}

export interface PolicyOut {
  weights: PolicyWeightOut[];
  total_pct: string;
  is_balanced: boolean;
}

export interface PolicyWeightIn {
  ticker: string;
  weight_pct: number;
  notes?: string | null;
}

export type TxClassification = "external_in" | "external_out" | "internal";

export interface InvestmentTransactionOut {
  plaid_investment_transaction_id: string;
  account_id: number;
  account_name: string;
  security_id: number | null;
  ticker: string | null;
  date: string;
  name: string | null;
  quantity: string;
  amount: string;
  price: string | null;
  fees: string | null;
  type: string;
  subtype: string | null;
  currency: string;
  override_classification: TxClassification | null;
  effective_classification: TxClassification | null;
}

export interface TransactionOverrideIn {
  plaid_investment_transaction_id: string;
  classification: TxClassification;
  notes?: string | null;
}

export interface TransactionOverrideOut {
  plaid_investment_transaction_id: string;
  classification: TxClassification;
  notes: string | null;
  updated_at: string;
  tx_date: string | null;
  tx_type: string | null;
  tx_subtype: string | null;
  tx_amount: string | null;
  account_name: string | null;
  ticker: string | null;
}

export interface LinkTokenOut {
  link_token: string;
}

export interface ExchangePublicTokenOut {
  item_id: number;
  accounts_linked: number;
}

// Positioning (Holdings positioning section)

export interface PositioningBucketOut {
  label: string;
  value: string;
  weight_pct: string;
  count: number;
}

export interface ConcentrationOut {
  num_positions: number;
  top1_weight_pct: string | null;
  top5_weight_pct: string | null;
  top10_weight_pct: string | null;
  hhi: number | null;
  effective_holdings: number | null;
}

export interface PositionCorrelationRow {
  security_id: number;
  ticker: string | null;
  name: string | null;
  value: string;
  weight_pct: string;
  sample_size: number;
  correlation_spy: number | null;
  beta_spy: number | null;
  correlation_qqq: number | null;
  beta_qqq: number | null;
  correlation_policy: number | null;
  beta_policy: number | null;
}

export interface PositioningOut {
  snapshot_date: string;
  start_date: string;
  end_date: string;
  total_value: string;
  by_asset_type: PositioningBucketOut[];
  by_sector: PositioningBucketOut[];
  by_region: PositioningBucketOut[];
  by_account_type: PositioningBucketOut[];
  concentration: ConcentrationOut;
  correlations: PositionCorrelationRow[];
  weighted_avg_correlation_spy: number | null;
  has_policy: boolean;
  notes: string[];
}

export interface SecurityClassificationOut {
  security_id: number;
  ticker: string | null;
  security_name: string | null;
  sector: string | null;
  region: string | null;
  source: string;
  notes: string | null;
  updated_at: string;
}

export interface CashflowRuleGroupOut {
  decision_source: "override" | "name" | "sign" | "subtype";
  classification: TxClassification;
  reason: string;
  type: string;
  subtype: string | null;
  distinct_patterns: number;
  count: number;
  net_cashflow: string;
  gross_amount: string;
  first_date: string | null;
  last_date: string | null;
  accounts: string[];
  sample_names: string[];
  transaction_ids: string[];
  counts_toward_return: boolean;
}

export interface CashflowRuleAuditOut {
  start_date: string | null;
  end_date: string | null;
  groups: CashflowRuleGroupOut[];
  net_external_cashflow_in: string;
}
