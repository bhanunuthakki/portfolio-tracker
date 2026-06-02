import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";

import { Accounts } from "./pages/Accounts";
import { Cockpit } from "./pages/Cockpit";
import { Contributions } from "./pages/Contributions";
import { Dashboard } from "./pages/Dashboard";
import { Earnings } from "./pages/Earnings";
import { Holdings } from "./pages/Holdings";
import { TradeAnalysis } from "./pages/TradeAnalysis";
import { TradeTimeline } from "./pages/TradeTimeline";
import { Transactions } from "./pages/Transactions";

const navItems: { to: string; label: string }[] = [
  { to: "/", label: "Cockpit" },
  { to: "/dashboard", label: "Dashboard" },
  { to: "/holdings", label: "Holdings" },
  { to: "/transactions", label: "Transactions" },
  { to: "/contributions", label: "Contributions" },
  { to: "/trade-analysis", label: "Trade analysis" },
  { to: "/trade-timeline", label: "Trade timeline" },
  { to: "/earnings", label: "Earnings" },
  { to: "/accounts", label: "Accounts" },
];

type Theme = "light" | "dark";

/**
 * Light / dark toggle. Mirrors the Earnings Summary renderer's tweaks
 * panel — the whole palette flips by setting `data-theme` on <html>,
 * since every token is a CSS variable. Choice persists in localStorage;
 * the initial value is applied pre-paint by the inline script in
 * index.html, so this just keeps React in sync and writes changes back.
 */
function ThemeToggle(): JSX.Element {
  const [theme, setTheme] = useState<Theme>(
    () =>
      (typeof document !== "undefined" &&
        (document.documentElement.dataset.theme as Theme)) ||
      "light",
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("pt-theme", theme);
    } catch {
      /* private mode — ignore */
    }
  }, [theme]);

  const next = theme === "light" ? "dark" : "light";
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
      className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-line bg-surface text-muted transition-colors hover:border-line-strong hover:text-ink"
    >
      {theme === "light" ? <MoonIcon /> : <SunIcon />}
    </button>
  );
}

function MoonIcon(): JSX.Element {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SunIcon(): JSX.Element {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function App(): JSX.Element {
  return (
    <div className="flex min-h-dvh flex-col bg-canvas text-ink">
      <header className="sticky top-0 z-30 border-b border-line bg-surface/85 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-x-6 gap-y-2 px-6 py-3">
          <NavLink
            to="/"
            className="shrink-0 font-mono text-[13.5px] font-semibold tracking-tight text-ink"
          >
            portfolio<span className="mx-0.5 text-faint">/</span>tracker
          </NavLink>
          <nav className="flex flex-1 flex-wrap items-center gap-x-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  [
                    "border-b-2 px-2 pb-1 pt-1 text-[13px] font-medium transition-colors",
                    isActive
                      ? "border-ink text-ink"
                      : "border-transparent text-muted hover:text-ink-soft",
                  ].join(" ")
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <ThemeToggle />
        </div>
      </header>
      <main className="flex-1">
        <div className="mx-auto max-w-7xl px-6 py-7">
          <Routes>
            <Route path="/" element={<Cockpit />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/holdings" element={<Holdings />} />
            <Route path="/transactions" element={<Transactions />} />
            <Route path="/contributions" element={<Contributions />} />
            <Route path="/trade-analysis" element={<TradeAnalysis />} />
            <Route path="/trade-timeline" element={<TradeTimeline />} />
            <Route path="/earnings" element={<Earnings />} />
            <Route path="/accounts" element={<Accounts />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
