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
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
}): JSX.Element {
  return (
    <td
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

// ---- helpers ------------------------------------------------------------

/** Tailwind class for positive/negative tabular numbers. */
export function pnlClass(value: number | null): string {
  if (value === null) return "";
  if (value > 0) return "text-emerald-700";
  if (value < 0) return "text-red-700";
  return "";
}

/** "$1,234" — rounded, US formatting. */
export function fmtUSD(value: number | null, fractionDigits: number = 0): string {
  if (value === null) return "—";
  return `$${value.toLocaleString(undefined, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  })}`;
}

/** "+$1,234" / "-$1,234" / "—". Sign always precedes `$`. */
export function fmtSignedUSD(value: number | null): string {
  if (value === null) return "—";
  const sign = value >= 0 ? "+" : "-";
  return `${sign}$${Math.round(Math.abs(value)).toLocaleString()}`;
}
