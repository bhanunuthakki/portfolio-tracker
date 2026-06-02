/** @type {import('tailwindcss').Config} */

// Wrap a channel variable so Tailwind can still inject opacity modifiers
// (e.g. `bg-paper/60`). Every color below resolves to `rgb(R G B / alpha)`.
const c = (v) => `rgb(var(${v}) / <alpha-value>)`;

// Remapped Tailwind ramp → editorial channel variables. The app was built
// on `slate`/`emerald`/`red`/`amber`/`indigo`/`sky`/`blue`; pointing those
// families at the editorial palette re-skins every existing component
// without touching its class names, and collapses the old rainbow of
// accents down to one disciplined blue.
const ramp = (p) => ({
  50: c(`--${p}-50`),
  100: c(`--${p}-100`),
  200: c(`--${p}-200`),
  300: c(`--${p}-300`),
  400: c(`--${p}-400`),
  500: c(`--${p}-500`),
  600: c(`--${p}-600`),
  700: c(`--${p}-700`),
  800: c(`--${p}-800`),
  900: c(`--${p}-900`),
});

// Some ramps only define the shades the codebase actually uses; fill the
// gaps by reusing neighbors so arbitrary shades never resolve to nothing.
const accentRamp = {
  50: c("--ac-50"),
  100: c("--ac-100"),
  200: c("--ac-200"),
  300: c("--ac-300"),
  400: c("--ac-600"),
  500: c("--ac-600"),
  600: c("--ac-600"),
  700: c("--ac-700"),
  800: c("--ac-800"),
  900: c("--ac-900"),
};
const skyRamp = {
  50: c("--sk-50"),
  100: c("--sk-100"),
  200: c("--sk-200"),
  300: c("--sk-200"),
  400: c("--sk-600"),
  500: c("--sk-600"),
  600: c("--sk-600"),
  700: c("--sk-700"),
  800: c("--sk-800"),
  900: c("--sk-800"),
};
const gainRamp = {
  50: c("--gn-50"),
  100: c("--gn-100"),
  200: c("--gn-200"),
  300: c("--gn-200"),
  400: c("--gn-600"),
  500: c("--gn-600"),
  600: c("--gn-600"),
  700: c("--gn-700"),
  800: c("--gn-800"),
  900: c("--gn-900"),
};
const lossRamp = {
  50: c("--ls-50"),
  100: c("--ls-100"),
  200: c("--ls-200"),
  300: c("--ls-200"),
  400: c("--ls-600"),
  500: c("--ls-600"),
  600: c("--ls-600"),
  700: c("--ls-700"),
  800: c("--ls-800"),
  900: c("--ls-900"),
};
const warnRamp = {
  50: c("--wn-50"),
  100: c("--wn-100"),
  200: c("--wn-200"),
  300: c("--wn-200"),
  400: c("--wn-600"),
  500: c("--wn-600"),
  600: c("--wn-600"),
  700: c("--wn-700"),
  800: c("--wn-800"),
  900: c("--wn-900"),
};

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        // Routed through a CSS variable (see --serif in index.css) because
        // Tailwind's dev build emits the trailing-digit family "Source Serif
        // 4" unquoted, which is invalid CSS and drops the declaration.
        serif: ["var(--serif)"],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      colors: {
        // Theme-flipping white (see --white in index.css): keeps every
        // existing `bg-white` / `text-white` coherent in dark mode without
        // touching component code.
        white: c("--white"),

        // Remap the legacy families onto the editorial palette.
        slate: ramp("n"),
        emerald: gainRamp,
        red: lossRamp,
        rose: lossRamp,
        amber: warnRamp,
        indigo: accentRamp,
        blue: accentRamp,
        sky: skyRamp,

        // Semantic tokens (preferred for new/refactored code).
        canvas: c("--canvas"),
        surface: c("--surface"),
        paper: {
          DEFAULT: c("--paper"),
          2: c("--paper-2"),
        },
        hairline: c("--hairline"),
        line: {
          DEFAULT: c("--line"),
          strong: c("--line-strong"),
        },
        ink: {
          DEFAULT: c("--ink"),
          soft: c("--ink-soft"),
        },
        body: c("--body"),
        muted: c("--muted"),
        faint: c("--faint"),
        accent: {
          DEFAULT: c("--accent"),
          strong: c("--accent-strong"),
          soft: c("--accent-soft"),
        },
        gain: {
          DEFAULT: c("--gn-700"),
          soft: c("--gn-100"),
          strong: c("--gn-800"),
        },
        loss: {
          DEFAULT: c("--ls-700"),
          soft: c("--ls-100"),
          strong: c("--ls-800"),
        },
        warn: {
          DEFAULT: c("--wn-700"),
          soft: c("--wn-100"),
          strong: c("--wn-800"),
        },
        tone: {
          pos: c("--tone-pos"),
          neu: c("--tone-neu"),
          warn: c("--tone-warn"),
          neg: c("--tone-neg"),
        },
      },
      boxShadow: {
        card: "var(--shadow-card)",
        pop: "var(--shadow-pop)",
      },
      letterSpacing: {
        tightest: "-0.045em",
      },
    },
  },
  plugins: [],
};
