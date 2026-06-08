// Build script for the Privy embedded-wallet bundles.
//
// Bundles each entry (React + @privy-io/react-auth + viem) into a single,
// self-contained IIFE committed under ../../static/js/. This mirrors the
// existing "commit a pre-built bundle" pattern used by static/js/wc-bundle.js,
// so the Flask app needs no Node build step at runtime.
//
//   src/index.jsx  -> static/js/privy-embedded-wallet.js  (standalone POC page)
//   src/wallet.jsx -> static/js/privy-wallet.js           (window.GMPrivy bridge)
//
// Usage:  npm install && npm run build   (from frontend/privy/)

import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));

const ENTRIES = [
  { entry: "src/index.jsx", out: "../../static/js/privy-embedded-wallet.js" },
  { entry: "src/wallet.jsx", out: "../../static/js/privy-wallet.js" },
];

for (const { entry, out } of ENTRIES) {
  const outfile = resolve(__dirname, out);
  await build({
    entryPoints: [resolve(__dirname, entry)],
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
}
