// Build script for the Privy embedded-wallet POC bundle.
//
// Bundles src/index.jsx (React + @privy-io/react-auth + viem) into a single,
// self-contained IIFE that is committed to ../../static/js/privy-embedded-wallet.js
// and mounted by templates/embedded_wallet.html. This mirrors the existing
// "commit a pre-built bundle" pattern used by static/js/wc-bundle.js, so the
// Flask app needs no Node build step at runtime.
//
// Usage:  npm install && npm run build   (from frontend/privy/)

import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const outfile = resolve(__dirname, "../../static/js/privy-embedded-wallet.js");

await build({
  entryPoints: [resolve(__dirname, "src/index.jsx")],
  outfile,
  bundle: true,
  minify: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  jsx: "automatic",
  sourcemap: false,
  legalComments: "none",
  define: {
    "process.env.NODE_ENV": '"production"',
    global: "globalThis",
  },
  logLevel: "info",
});

console.log(`[privy-build] wrote ${outfile}`);
