/**
 * Tiny design-system primitives.
 *
 * Every page should use these instead of inlining one-off Tailwind classes.
 * The goal is visual consistency: one card style, one button style per
 * intent, one table-cell rhythm. Add new variants here, not in pages.
 *
 * Type system:
 *   12 → text-[11px] uppercase tracking-wider     (stat labels)
 *   xs → text-xs                                   (help, captions)
 *   sm → text-sm                                   (body / table cells)
 *   md → text-base font-medium                     (section titles)
 *   lg → text-lg font-semibold tabular-nums        (stat values)
 *
 * Color palette (via Tailwind):
 *   text:    slate-900 (primary), slate-700 (body), slate-500 (muted)
 *   border:  slate-200 (cards/inputs), slate-100 (dividers)
 *   bg:      white (cards), slate-50 (page, tables)
 *   accent:  slate-900 → slate-700 hover (primary action)
 *   gain:    emerald-700
 *   loss:    red-700
 */

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

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
        "rounded-lg border border-slate-200 bg-white",
        className ?? "",
      ].join(" ")}
    >
      {children}
    </div>
  );
}

export function SectionHeader({
  title,
  actions,
}: {
  title: string;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <header className="flex flex-col gap-3 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function PageHeader({
  title,
  actions,
}: {
  title: string;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <h2 className="text-base font-semibold text-slate-900">{title}</h2>
      {actions && (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      )}
    </div>
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
      className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
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
      className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
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
      className="text-xs text-red-600 hover:text-red-800"
    >
      {children}
    </button>
  );
}

// ---- stats --------------------------------------------------------------

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
    <Card className="px-4 py-3">
      <div className="text-[11px] uppercase tracking-wider text-slate-500">
        {label}
      </div>
      <div
        className={[
          "mt-1 truncate font-semibold tabular-nums text-slate-900",
          mono ? "font-mono text-sm" : "text-lg",
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
        "px-4 py-2 text-xs font-medium uppercase tracking-wide text-slate-500",
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
        "px-4 py-2 text-sm text-slate-700",
        align === "right" ? "text-right" : "text-left",
        className ?? "",
      ].join(" ")}
    >
      {children}
    </td>
  );
}

// ---- banners / states ---------------------------------------------------

export function EmptyState({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
      {children}
    </div>
  );
}

export function ErrorBanner({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
      {children}
    </div>
  );
}

export function InfoBanner({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
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
    <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
      {title && <div className="font-medium">{title}</div>}
      <div className={title ? "mt-1 text-xs leading-relaxed" : ""}>{children}</div>
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
        className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-slate-300 text-[9px] font-bold normal-case text-slate-500 hover:border-slate-500 hover:text-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-slate-700"
      >
        ?
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={`${label} explainer`}
          className="absolute left-0 top-full z-20 mt-1.5 w-80 max-w-[calc(100vw-2rem)] rounded-md border border-slate-200 bg-white p-3 text-left text-xs normal-case tracking-normal text-slate-700 shadow-lg"
        >
          <div className="text-sm font-semibold text-slate-900">{label}</div>
          <p className="mt-1.5 leading-relaxed">{explainer.definition}</p>
          {explainer.formula && (
            <div className="mt-2.5">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">
                Formula
              </div>
              <div className="mt-1 rounded bg-slate-50 px-2 py-1 font-mono text-[11px] text-slate-800">
                {explainer.formula}
              </div>
            </div>
          )}
          {explainer.interpretation && (
            <div className="mt-2.5">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">
                Reading it
              </div>
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
      className="group rounded-md border border-slate-200 bg-slate-50/60 px-3 py-2 text-xs text-slate-600"
    >
      <summary className="cursor-pointer list-none [&::-webkit-details-marker]:hidden flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500 hover:text-slate-700">
        <span className="text-slate-400 transition-transform group-open:rotate-90">▸</span>
        {title}
      </summary>
      <div className="mt-2 leading-relaxed">{children}</div>
    </details>
  );
}

// ---- helpers ------------------------------------------------------------

/** Tailwind class for positive/negative tabular numbers. */
export function pnlClass(value: number | null): string {
  if (value === null) return "";
  if (value > 0) return "text-emerald-700";
  if (value < 0) return "text-red-700";
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
