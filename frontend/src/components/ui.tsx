/**
 * Tiny design-system primitives — "paper on monochrome" editorial style.
 *
 * Ported from the Earnings Summary workspace renderer: a warm paper canvas,
 * white panels, hairline rules, a single blue accent, mono figures + mono
 * uppercase "eyebrow" labels, and disciplined gain/loss/warn semantics.
 *
 * Every page should use these instead of inlining one-off Tailwind classes.
 * The goal is visual consistency: one panel style, one button per intent,
 * one table-cell rhythm. Add new variants here, not in pages.
 *
 * Color tokens (all theme-aware via CSS variables — see index.css):
 *   surface · paper · canvas          backgrounds (card / inset / page)
 *   ink · ink-soft · body · muted · faint   text, darkest → lightest
 *   line · line-strong · hairline      borders, faint → strong
 *   accent (+ soft / strong)           the single editorial blue
 *   gain · loss · warn (+ soft/strong) P&L + status semantics
 *
 * TABLE CONVENTION: every data table is sortable on every column, asc +
 * desc. Use <SortableTh> (below) backed by a useTableSort() controller for
 * headers — never a bespoke clickable <th>. This is a standing requirement
 * for all current and future tables. Editable config grids (PolicyEditor,
 * HumanCapitalEditor) are the only exception — their rows are inputs, not
 * data, so a plain <Th> is correct there.
 *
 * Type scale:
 *   eyebrow → mono 10.5px uppercase tracked   (labels, kickers)
 *   xs → text-xs                              (help, captions)
 *   sm → text-sm                              (body / table cells)
 *   figure → font-mono tabular-nums           (every number)
 */

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import type { TxClassification } from "@/types";

// ---- cashflow classification (shared by Transactions + Contributions) ---

export const CLASSIFICATION_LABELS: Record<TxClassification, string> = {
  external_in: "Contribution",
  external_out: "Withdrawal",
  internal: "Internal",
};

export const CLASSIFICATION_CHIP_CLASSES: Record<TxClassification, string> = {
  external_in: "bg-gain-soft text-gain-strong",
  external_out: "bg-loss-soft text-loss-strong",
  internal: "bg-paper-2 text-body",
};

// ---- containers ---------------------------------------------------------

export function Card({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}): JSX.Element {
  return (
    <div
      className={[
        "rounded-lg border border-line bg-surface shadow-card",
        className ?? "",
      ].join(" ")}
    >
      {children}
    </div>
  );
}

/**
 * Editorial panel with a hairline-ruled header. Use for any titled section
 * (chart, table, list). `eyebrow` renders the mono kicker above the title;
 * `actions` sits flush-right in the header; `footer` gets the paper-toned
 * note strip at the bottom.
 */
export function Panel({
  title,
  eyebrow,
  sub,
  actions,
  footer,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  eyebrow?: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}): JSX.Element {
  return (
    <Card className={["overflow-hidden", className ?? ""].join(" ")}>
      {(title || actions || eyebrow) && (
        <header className="flex flex-col gap-3 border-b border-hairline px-4 py-3 sm:flex-row sm:items-baseline sm:justify-between">
          <div className="min-w-0">
            {eyebrow && <div className="eyebrow mb-1">{eyebrow}</div>}
            {title && (
              <h2 className="text-sm font-semibold tracking-tight text-ink">
                {title}
              </h2>
            )}
            {sub && <p className="mt-0.5 text-xs text-muted">{sub}</p>}
          </div>
          {actions && (
            <div className="flex flex-wrap items-center gap-2">{actions}</div>
          )}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
      {footer && (
        <div className="border-t border-hairline bg-paper px-4 py-2.5 text-xs leading-relaxed text-muted">
          {footer}
        </div>
      )}
    </Card>
  );
}

export function PageHeader({
  title,
  eyebrow,
  actions,
}: {
  title: string;
  eyebrow?: string;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        {eyebrow && <div className="eyebrow mb-1.5">{eyebrow}</div>}
        <h2 className="text-xl font-semibold tracking-tight text-ink">
          {title}
        </h2>
      </div>
      {actions && (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      )}
    </div>
  );
}

// ---- typography ---------------------------------------------------------

/** Mono uppercase kicker that sits above a title or stat. */
export function Eyebrow({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}): JSX.Element {
  return <div className={["eyebrow", className ?? ""].join(" ")}>{children}</div>;
}

/** Large editorial section title (sans, tight tracking). */
export function SectionTitle({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}): JSX.Element {
  return (
    <h2
      className={[
        "text-2xl font-semibold tracking-tight text-ink",
        className ?? "",
      ].join(" ")}
    >
      {children}
    </h2>
  );
}

// ---- buttons ------------------------------------------------------------

type ButtonProps = {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  disabled?: boolean;
};

export function PrimaryButton({
  children,
  onClick,
  type = "button",
  disabled,
}: ButtonProps): JSX.Element {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className="rounded-md bg-ink px-3 py-1.5 text-sm font-medium text-canvas transition-colors hover:bg-ink-soft disabled:cursor-not-allowed disabled:opacity-50"
    >
      {children}
    </button>
  );
}

export function SecondaryButton({
  children,
  onClick,
  type = "button",
  disabled,
}: ButtonProps): JSX.Element {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className="rounded-md border border-line-strong bg-surface px-3 py-1.5 text-sm font-medium text-ink-soft transition-colors hover:border-line-strong hover:bg-paper disabled:cursor-not-allowed disabled:opacity-50"
    >
      {children}
    </button>
  );
}

export function DangerLink({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick?: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className="text-xs text-loss transition-colors hover:text-loss-strong"
    >
      {children}
    </button>
  );
}

// ---- pills / chips ------------------------------------------------------

type PillTone = "accent" | "neutral" | "muted" | "warn" | "gain" | "loss";

const PILL_TONE: Record<PillTone, string> = {
  accent: "bg-accent-soft text-accent border-accent/30",
  neutral: "bg-paper text-ink-soft border-line",
  muted: "bg-paper text-muted border-line",
  warn: "bg-tone-warn text-warn-strong border-warn/30",
  gain: "bg-gain-soft text-gain-strong border-gain/30",
  loss: "bg-tone-neg text-loss-strong border-loss/30",
};

/** Small mono status pill. */
export function Pill({
  children,
  tone = "neutral",
  className,
  title,
}: {
  children: ReactNode;
  tone?: PillTone;
  className?: string;
  title?: string;
}): JSX.Element {
  return (
    <span
      title={title}
      className={[
        "inline-flex items-center gap-1 rounded border px-2 py-0.5 font-mono text-[10.5px] font-medium tracking-wide",
        PILL_TONE[tone],
        className ?? "",
      ].join(" ")}
    >
      {children}
    </span>
  );
}

// ---- stats --------------------------------------------------------------

/**
 * KPI tile — mono figure over a mono-uppercase label. `mono` flags a long
 * value (e.g. a date range) that should render a notch smaller so it fits
 * on one line; the figure is always mono + tabular.
 */
export function Stat({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}): JSX.Element {
  return (
    <Card className="px-5 py-4">
      <div className="eyebrow min-h-[26px] leading-[1.3]">{label}</div>
      <div
        className={[
          "mt-1.5 truncate font-mono font-medium tracking-tight tabular-nums text-ink",
          mono ? "text-[15px]" : "text-2xl",
        ].join(" ")}
        title={value}
      >
        {value}
      </div>
    </Card>
  );
}

// ---- tables -------------------------------------------------------------

export function Th({
  children,
  align,
}: {
  children: ReactNode;
  align?: "left" | "right";
}): JSX.Element {
  return (
    <th
      scope="col"
      className={[
        "px-4 py-2.5 text-[10.5px] font-medium uppercase tracking-[0.06em] text-muted",
        align === "right" ? "text-right" : "text-left",
      ].join(" ")}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  align,
  className,
  title,
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
  title?: string;
}): JSX.Element {
  return (
    <td
      title={title}
      className={[
        "px-4 py-2.5 text-sm text-body",
        align === "right" ? "text-right tabular-nums" : "text-left",
        className ?? "",
      ].join(" ")}
    >
      {children}
    </td>
  );
}

/**
 * Minimal sort state a <SortableTh> needs. The object returned by
 * `useTableSort` satisfies this structurally, so pass it straight through:
 *
 *   const sort = useTableSort(rows, "date", "desc", accessors);
 *   <SortableTh column="date" sort={sort}>Date</SortableTh>
 */
export interface SortControl {
  sortKey: string;
  sortDir: "asc" | "desc";
  toggle: (key: string) => void;
}

/**
 * Clickable, keyboard-operable column header with an asc/desc indicator.
 *
 * THE canonical sortable header — see the TABLE CONVENTION note at the top
 * of this file. Renders the label inside a <button> (so it's reachable by
 * keyboard) and sets `aria-sort` for screen readers. Inactive columns show
 * a faint ↕ so it's obvious every column is sortable.
 *
 * `pad` matches the header padding to the table's body cells: "normal"
 * (px-4) aligns with <Td>; "tight" (px-3) aligns with the denser
 * `text-xs` tables. `aside` renders OUTSIDE the sort button (e.g. an
 * <InfoButton>) so its own click doesn't trigger a sort — and so we never
 * nest a <button> inside a <button>.
 */
export function SortableTh({
  column,
  children,
  sort,
  align = "left",
  pad = "normal",
  aside,
}: {
  column: string;
  children: ReactNode;
  sort: SortControl;
  align?: "left" | "right";
  pad?: "normal" | "tight";
  aside?: ReactNode;
}): JSX.Element {
  const active = sort.sortKey === column;
  const arrow = !active ? "↕" : sort.sortDir === "asc" ? "↑" : "↓";
  const label = typeof children === "string" ? children : column;
  return (
    <th
      scope="col"
      aria-sort={
        active ? (sort.sortDir === "asc" ? "ascending" : "descending") : "none"
      }
      className={[
        pad === "tight" ? "px-3 py-2" : "px-4 py-2.5",
        "text-[10.5px] font-medium uppercase tracking-[0.06em] text-muted",
      ].join(" ")}
    >
      <span
        className={[
          "flex w-full items-center gap-1",
          align === "right" ? "justify-end" : "justify-start",
        ].join(" ")}
      >
        <button
          type="button"
          onClick={() => sort.toggle(column)}
          aria-label={`Sort by ${label}`}
          className="inline-flex select-none items-center gap-1 rounded uppercase tracking-[0.06em] transition-colors hover:text-ink"
        >
          {children}
          <span
            aria-hidden="true"
            className={[
              "text-[10px]",
              active ? "text-ink-soft" : "text-faint",
            ].join(" ")}
          >
            {arrow}
          </span>
        </button>
        {aside}
      </span>
    </th>
  );
}

// ---- banners / states ---------------------------------------------------

export function EmptyState({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="rounded-lg border border-dashed border-line-strong bg-surface p-8 text-center text-sm text-muted">
      {children}
    </div>
  );
}

export function ErrorBanner({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="rounded-md border border-loss/30 bg-tone-neg px-3 py-2 text-sm text-loss-strong">
      {children}
    </div>
  );
}

export function InfoBanner({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="rounded-md border border-line bg-paper px-3 py-2 text-sm text-body">
      {children}
    </div>
  );
}

export function WarningBanner({
  title,
  children,
}: {
  title?: string;
  children: ReactNode;
}): JSX.Element {
  return (
    <div className="rounded-md border border-warn/30 bg-tone-warn px-3 py-2 text-sm text-warn-strong">
      {title && <div className="font-medium">{title}</div>}
      <div className={title ? "mt-1 text-xs leading-relaxed" : ""}>
        {children}
      </div>
    </div>
  );
}

// ---- methodology disclosure --------------------------------------------

export interface MethodologyExplainer {
  definition: string;
  formula?: string;
  interpretation?: string;
}

/**
 * Click-toggle `(?)` popover for surfacing definitions inline. Hover is
 * deliberately NOT used — multi-paragraph methodology text doesn't fit
 * native tooltips, and hover is inaccessible on touch. Closes on outside
 * click or Escape.
 *
 * Use this for any value that has a methodology behind it that the user
 * should be able to read without leaving the page. Pair with a label so
 * the icon never floats alone.
 */
export function InfoButton({
  label,
  explainer,
}: {
  label: string;
  explainer: MethodologyExplainer;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative inline-flex">
      <button
        type="button"
        aria-label={`What is ${label}?`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-line-strong text-[9px] font-bold normal-case text-muted transition-colors hover:border-muted hover:text-ink"
      >
        ?
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={`${label} explainer`}
          className="absolute left-0 top-full z-20 mt-1.5 w-80 max-w-[calc(100vw-2rem)] rounded-md border border-line bg-surface p-3 text-left text-xs normal-case tracking-normal text-body shadow-pop"
        >
          <div className="text-sm font-semibold text-ink">{label}</div>
          <p className="mt-1.5 leading-relaxed">{explainer.definition}</p>
          {explainer.formula && (
            <div className="mt-2.5">
              <div className="eyebrow">Formula</div>
              <div className="mt-1 rounded bg-paper px-2 py-1 font-mono text-[11px] text-ink-soft">
                {explainer.formula}
              </div>
            </div>
          )}
          {explainer.interpretation && (
            <div className="mt-2.5">
              <div className="eyebrow">Reading it</div>
              <p className="mt-1 leading-relaxed">{explainer.interpretation}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Collapsible "Methodology" disclosure block. Use beneath a chart or
 * table to make the bookkeeping behind a number readable without bloating
 * the default view. Defaults closed.
 */
export function MethodologyNote({
  title,
  children,
  defaultOpen = false,
}: {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
}): JSX.Element {
  return (
    <details
      open={defaultOpen}
      className="group rounded-md border border-line bg-paper/60 px-3 py-2 text-xs text-muted"
    >
      <summary className="flex cursor-pointer list-none items-center gap-1.5 [&::-webkit-details-marker]:hidden">
        <span className="font-mono text-faint transition-transform group-open:rotate-90">
          ▸
        </span>
        <span className="eyebrow">{title}</span>
      </summary>
      <div className="mt-2 leading-relaxed text-body">{children}</div>
    </details>
  );
}

// ---- helpers ------------------------------------------------------------

/** Tailwind class for positive/negative tabular numbers. */
export function pnlClass(value: number | null): string {
  if (value === null) return "";
  if (value > 0) return "text-gain";
  if (value < 0) return "text-loss";
  return "";
}

/**
 * "$1,234" — rounded, US formatting.
 *
 * Project convention: dollar amounts have NO decimal points. The
 * `fractionDigits` argument defaults to 0 and callers should NOT pass
 * a non-zero value for dollar displays. (Per-share prices, EPS, and
 * percentages aren't covered by this rule — those use their own
 * formatters where decimals are still acceptable.)
 */
export function fmtUSD(value: number | null, fractionDigits: number = 0): string {
  if (value === null) return "—";
  return `$${value.toLocaleString(undefined, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  })}`;
}

/**
 * "+$1,234" / "-$1,234" / "—". Sign always precedes `$`.
 *
 * Always whole-dollar — same convention as `fmtUSD`. No knob to add
 * decimals here on purpose.
 */
export function fmtSignedUSD(value: number | null): string {
  if (value === null) return "—";
  const sign = value >= 0 ? "+" : "-";
  return `${sign}$${Math.round(Math.abs(value)).toLocaleString()}`;
}
