// Design system (Google Stitch, rodada 2) adaptado: cores por variáveis CSS (tema claro/escuro), raios maiores.
// Compilado pelo Tailwind CLI standalone (tools/build-css.sh) para evprices/web/static/app.css — sem Node no runtime.
const rgb = (v) => `rgb(var(--${v}) / <alpha-value>)`;
const tokens = [
  "background", "on-background", "surface", "surface-dim", "surface-bright", "surface-variant", "on-surface", "on-surface-variant",
  "surface-container-lowest", "surface-container-low", "surface-container", "surface-container-high", "surface-container-highest",
  "inverse-surface", "inverse-on-surface", "inverse-primary", "surface-tint", "outline", "outline-variant",
  "primary", "on-primary", "primary-container", "on-primary-container", "primary-fixed", "primary-fixed-dim", "on-primary-fixed", "on-primary-fixed-variant",
  "secondary", "on-secondary", "secondary-container", "on-secondary-container", "secondary-fixed", "secondary-fixed-dim", "on-secondary-fixed", "on-secondary-fixed-variant",
  "tertiary", "on-tertiary", "tertiary-container", "on-tertiary-container", "tertiary-fixed", "tertiary-fixed-dim", "on-tertiary-fixed", "on-tertiary-fixed-variant",
  "error", "on-error", "error-container", "on-error-container",
];
const colors = Object.fromEntries(tokens.map((t) => [t, rgb(t)]));

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./evprices/web/templates/**/*.html", "./evprices/web/static/*.js"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors,
      borderRadius: { DEFAULT: "6px", md: "8px", lg: "10px", xl: "14px", "2xl": "18px" },
      spacing: {
        "space-xs": "0.25rem", "space-sm": "0.5rem", "space-md": "1rem", "space-lg": "1.5rem", "space-xl": "2rem",
        margin: "1rem", "margin-tablet": "1.5rem", "margin-desktop": "2rem", gutter: "1rem", "gutter-desktop": "1.5rem",
      },
      fontFamily: {
        display: ["Space Grotesk", "system-ui", "sans-serif"],
        body: ["Geist", "system-ui", "sans-serif"],
        "headline-xl": ["Space Grotesk"], "headline-lg": ["Space Grotesk"], "headline-md": ["Space Grotesk"],
        "headline-xl-mobile": ["Space Grotesk"], "headline-lg-mobile": ["Space Grotesk"],
        "price-display": ["Space Grotesk"], "price-display-mobile": ["Space Grotesk"],
        "body-lg": ["Geist"], "body-md": ["Geist"], "body-sm": ["Geist"], "label-md": ["Geist"], "label-sm": ["Geist"],
        "telemetry-metric": ["Geist"],
      },
      fontSize: {
        "headline-xl": ["36px", { lineHeight: "44px", letterSpacing: "-0.03em", fontWeight: "700" }],
        "headline-xl-mobile": ["28px", { lineHeight: "34px", letterSpacing: "-0.02em", fontWeight: "700" }],
        "headline-lg": ["28px", { lineHeight: "36px", letterSpacing: "-0.02em", fontWeight: "600" }],
        "headline-lg-mobile": ["22px", { lineHeight: "28px", letterSpacing: "-0.01em", fontWeight: "600" }],
        "headline-md": ["20px", { lineHeight: "26px", letterSpacing: "-0.01em", fontWeight: "600" }],
        "price-display": ["32px", { lineHeight: "36px", letterSpacing: "-0.03em", fontWeight: "700" }],
        "price-display-mobile": ["24px", { lineHeight: "28px", letterSpacing: "-0.02em", fontWeight: "700" }],
        "telemetry-metric": ["18px", { lineHeight: "22px", fontWeight: "600" }],
        "body-lg": ["16px", { lineHeight: "24px", fontWeight: "400" }],
        "body-md": ["14px", { lineHeight: "20px", fontWeight: "400" }],
        "body-sm": ["12px", { lineHeight: "16px", fontWeight: "400" }],
        "label-md": ["12px", { lineHeight: "16px", letterSpacing: "0.04em", fontWeight: "600" }],
        "label-sm": ["11px", { lineHeight: "14px", letterSpacing: "0.06em", fontWeight: "700" }],
      },
    },
  },
  plugins: [],
};
