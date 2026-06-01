/**
 * GoodMarket — LI.FI / Jumper widget mount
 * ---------------------------------------------------------------------------
 * Renders the @lifi/widget React component into #lifiWidgetRoot on the
 * /swap "Buy Crypto" tab.  LI.FI's widget handles its own wallet connection
 * (injected EIP-1193 wallets + WalletConnect via wagmi), so users can
 * connect from inside the widget regardless of how they signed in to
 * GoodMarket itself.  The widget supports cross-chain swaps between many
 * chains; defaults target Celo → native ETH on Base but the user can
 * change source/destination from inside the widget UI.
 *
 * The widget is loaded from a single locally-vendored ESM bundle at
 * /static/js/vendor/lifi-widget.bundle.js (see tools/lifi-bundler/) so
 * the user only ever waits on ONE HTTP request to get the entire widget
 * runtime instead of waterfalling ~1,200 separate ES modules through
 * esm.sh, which previously caused multi-second cold loads + mid-load
 * failures bubbling up as "failed bridging/swapping".
 *
 * Exposes window.GMLifiReactWidget = { refresh() } so the host page can
 * re-render the widget after layout changes (e.g. tab switching).
 */
// IMPORTANT: We deliberately load LI.FI / Jumper from a single locally-
// vendored ESM bundle instead of esm.sh.  esm.sh waterfalls @lifi/widget
// into ~1,200 separate module requests (~4.5MB uncompressed) which made
// the Buy Crypto pane take 20-30s+ to load on slower connections AND
// caused mid-load failures that bubbled up to users as "bridging/
// swapping failed".  The vendored bundle ships React + ReactDOM + the
// widget in one file, so the browser only makes ONE HTTP request to get
// the entire widget runtime.  Build it with `npm --prefix
// tools/lifi-bundler run build` whenever the widget version is bumped.
import { LiFiWidget, React, ReactDOM } from "/static/js/vendor/lifi-widget.bundle.js";
const { useMemo } = React;
const { createRoot } = ReactDOM;

// LI.FI uses the zero address as the native-token sentinel on every
// supported EVM chain EXCEPT Celo, where the native CELO is itself an
// ERC-20 deployed at 0x471EcE...A438.  Using the zero address there
// trips LI.FI's API into returning `Token 42220-0x0000… is invalid or
// in deny list.` which kills the first gas/quote call and breaks the
// widget on load — the failure mode users reported as "failed
// bridging/swapping".  We pick per-chain defaults so the widget always
// boots with a valid sentinel even when the bootstrap config is
// missing or older.
const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";
const CELO_CHAIN_ID = 42220;
const CELO_NATIVE_TOKEN = "0x471EcE3750Da237f93B8E339c536989b8978a438";
function nativeTokenForChain(chainId) {
    return Number(chainId) === CELO_CHAIN_ID ? CELO_NATIVE_TOKEN : ZERO_ADDRESS;
}
const DEFAULT_STABILITY = Object.freeze({
    routePriority: "FASTEST",
    // Bumped from 0.01 (1%) — Celo bridges (Allbridge, Glacis, Eco) move
    // price 1–1.5% between quote and execution, which used to trip wallet
    // simulators with "Transaction will likely fail" / "unknown RPC error"
    // right at signing.
    slippage: 0.02,
    useRecommendedRoute: true,
    // LI.FI's default `RouteOptions.allowSwitchChain` is false, which hides
    // every Celo→Base bridge whose dest is a stable still needing an
    // on-Base swap (Allbridge, Glacis, Eco, Across via USDC …).  Without
    // those, the widget falls back to fragile single-tx routes that wallets
    // routinely reject.  Enable explicitly.
    allowSwitchChain: true,
    allowDestinationCall: true,
    // Permit2 (`callDiamondWithPermit2` at 0x89c6340B…) is LI.FI's default
    // signing path, but on Celo the native asset IS the CELO ERC-20 at
    // 0x471EcE…A438 — so the same token is moved twice on a single tx
    // (msg.value + Permit2 pull) and the wallet's pre-flight simulator
    // reverts.  Disabling message signing forces a standard `approve()`
    // flow that wallets simulate cleanly.
    disableMessageSigning: true,
});

function readBootstrap() {
    const node = document.getElementById("lifiWidgetBootstrap");
    if (!node) return {};
    try { return JSON.parse(node.textContent || "{}"); }
    catch (err) {
        console.error("[GoodMarket LI.FI] Invalid widget bootstrap JSON", err);
        return {};
    }
}

function isPresentWallet(address) {
    return Boolean(address && address !== "None" && /^0x[0-9a-fA-F]{40}$/.test(address));
}

function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\"": "&quot;",
        "'": "&#39;",
    }[ch]));
}

function renderFallbackMessage(message) {
    const rootEl = document.getElementById("lifiWidgetRoot");
    if (!rootEl) return;
    rootEl.innerHTML = "";
    const box = document.createElement("div");
    box.className = "lifi-react-status lifi-react-status--stack";
    box.setAttribute("data-connected", "false");
    box.style.marginBottom = "0.75rem";
    box.innerHTML = `
        <strong>⚠️ Buy Crypto widget unavailable</strong>
        <span>${escapeHtml(message)}</span>
        <button type="button" class="lifi-retry-btn" id="lifiRetryMountBtn">Reload LI.FI widget</button>`;
    rootEl.appendChild(box);
    const retry = document.getElementById("lifiRetryMountBtn");
    if (retry) retry.addEventListener("click", () => mount(true));
}

class WidgetErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false };
    }
    static getDerivedStateFromError() {
        return { hasError: true };
    }
    componentDidCatch(error) {
        console.error("[GoodMarket LI.FI] React widget render failed", error);
    }
    render() {
        if (this.state.hasError) {
            return React.createElement("div", { className: "lifi-react-status", "data-connected": "false" },
                React.createElement("strong", null, "⚠️ Buy Crypto widget failed to render"),
                React.createElement("span", null, "Please refresh the page. If this continues, reconnect your wallet and try again.")
            );
        }
        return this.props.children;
    }
}

function clampSlippage(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_STABILITY.slippage;
    return Math.min(Math.max(parsed, 0.001), 0.03);
}

function normalizeRoutePriority(value) {
    const normalized = String(value || DEFAULT_STABILITY.routePriority).toUpperCase();
    return normalized === "CHEAPEST" ? "CHEAPEST" : "FASTEST";
}

function normalizeBoolean(value, fallback) {
    if (typeof value === "boolean") return value;
    if (typeof value === "string") {
        if (["1", "true", "yes", "on"].includes(value.toLowerCase())) return true;
        if (["0", "false", "no", "off"].includes(value.toLowerCase())) return false;
    }
    return fallback;
}

function readRpcUrls(bootstrap) {
    const rpcUrls = bootstrap.rpcUrls && typeof bootstrap.rpcUrls === "object" ? bootstrap.rpcUrls : {};
    return Object.entries(rpcUrls).reduce((acc, [chainId, urls]) => {
        const cleaned = Array.isArray(urls)
            ? urls.map((url) => String(url || "").trim()).filter(Boolean)
            : [String(urls || "").trim()].filter(Boolean);
        if (cleaned.length) acc[Number(chainId)] = cleaned;
        return acc;
    }, {});
}

function makeConfig(bootstrap) {
    const integrator = bootstrap.integrator || "goodmarket-community";
    const fromChain = Number(bootstrap.fromChainId || 42220);
    const toChain = Number(bootstrap.toChainId || 8453);
    const fromToken = bootstrap.fromToken || nativeTokenForChain(fromChain);
    const toToken = bootstrap.toToken || nativeTokenForChain(toChain);
    const routePriority = normalizeRoutePriority(bootstrap.routePriority);
    const slippage = clampSlippage(bootstrap.slippage);
    const useRecommendedRoute = normalizeBoolean(bootstrap.useRecommendedRoute, DEFAULT_STABILITY.useRecommendedRoute);
    const allowSwitchChain = normalizeBoolean(bootstrap.allowSwitchChain, DEFAULT_STABILITY.allowSwitchChain);
    const allowDestinationCall = normalizeBoolean(bootstrap.allowDestinationCall, DEFAULT_STABILITY.allowDestinationCall);
    const disableMessageSigning = normalizeBoolean(bootstrap.disableMessageSigning, DEFAULT_STABILITY.disableMessageSigning);
    const rpcUrls = readRpcUrls(bootstrap);
    const walletConnectProjectId = typeof bootstrap.walletConnectProjectId === "string"
        ? bootstrap.walletConnectProjectId.trim()
        : "";

    const config = {
        integrator,
        variant: "compact",
        appearance: "dark",
        fromChain,
        toChain,
        fromToken,
        toToken,
        routePriority,
        slippage,
        useRecommendedRoute,
        buildUrl: true,
        // Unlocks multi-step Celo→Base routes whose 2nd step needs an
        // on-destination swap (Allbridge/Glacis/Eco …).  Without these,
        // LI.FI filters those bridges out and only fragile single-tx
        // routes are offered — the routes wallets reject as
        // "Transaction will likely fail (execution reverted)".
        routeOptions: {
            allowSwitchChain,
            allowDestinationCall,
        },
        // Bypass Permit2 signing so native CELO transfers don't reach the
        // `callDiamondWithPermit2` proxy at 0x89c6340B... which reverts in
        // wallet simulators because CELO's native + ERC-20 duality means
        // the same token is moved twice in one tx.
        executionOptions: {
            disableMessageSigning,
        },
        theme: {
            palette: {
                primary: { main: "#7c3aed" },
                secondary: { main: "#38bdf8" },
            },
            shape: {
                borderRadius: 14,
                borderRadiusSecondary: 10,
            },
            container: {
                boxShadow: "none",
                borderRadius: "16px",
            },
        },
    };

    const sdkConfig = {};
    if (bootstrap.apiUrl) sdkConfig.apiUrl = bootstrap.apiUrl;
    if (Object.keys(rpcUrls).length) sdkConfig.rpcUrls = rpcUrls;
    if (Object.keys(sdkConfig).length) config.sdkConfig = sdkConfig;

    // Forward our own WalletConnect projectId so LI.FI's internal wagmi WC
    // connector uses the same project (and shares one rate-limit /
    // metadata) instead of LI.FI's public default — the default
    // periodically fails `wallet_switchEthereumChain` with "An error
    // occurred when attempting to switch chain".
    if (walletConnectProjectId) {
        config.walletConfig = {
            walletConnect: {
                projectId: walletConnectProjectId,
                metadata: {
                    name: "GoodMarket",
                    description: "GoodMarket Community swap / bridge",
                    url: window.location?.origin || "https://goodmarket.live",
                    icons: ["https://goodmarket.live/static/images/favicon.png"],
                },
            },
        };
    }

    if (isPresentWallet(bootstrap.walletAddress)) {
        config.toAddress = {
            address: bootstrap.walletAddress,
            chainType: "EVM",
        };
    }

    return config;
}

function GoodMarketLifiWidget({ bootstrap, refreshKey }) {
    const config = useMemo(() => makeConfig(bootstrap), [bootstrap, refreshKey]);
    return React.createElement(React.Fragment, null,
        React.createElement("div", { className: "lifi-react-status lifi-react-status--stack", "data-connected": isPresentWallet(bootstrap.walletAddress) ? "true" : "false" },
            React.createElement("strong", null, "🛡️ LI.FI stability mode"),
            React.createElement("span", null, `Using ${config.routePriority.toLowerCase()}${config.useRecommendedRoute ? " recommended" : ""} routes with ${(config.slippage * 100).toFixed(1)}% slippage tolerance, multi-step bridges enabled, and Permit2 signing bypassed for Celo. If your wallet shows "Transaction will likely fail" or "An error occurred when attempting to switch chain", add the destination network (e.g. Base) manually in your wallet and refresh the widget below.`),
            React.createElement("button", { type: "button", className: "lifi-retry-btn", onClick: () => mount(true) }, "Refresh quote widget")
        ),
        React.createElement(LiFiWidget, {
            integrator: config.integrator,
            config,
        })
    );
}

let _root = null;
let _refreshKey = 0;

function mount(forceReset = false) {
    const rootEl = document.getElementById("lifiWidgetRoot");
    if (!rootEl) return;
    const bootstrap = readBootstrap();
    try {
        if (forceReset && _root) {
            _root.unmount();
            _root = null;
        }
        if (forceReset) _refreshKey += 1;
        if (!_root) _root = createRoot(rootEl);
        _root.render(
            React.createElement(WidgetErrorBoundary, { key: _refreshKey },
                React.createElement(GoodMarketLifiWidget, { bootstrap, refreshKey: _refreshKey })
            )
        );
        window.GMLifiReactWidget = {
            refresh: () => {
                try { mount(true); } catch (err) {
                    console.warn("[GoodMarket LI.FI] refresh failed", err);
                }
            },
        };
    } catch (err) {
        console.error("[GoodMarket LI.FI] Widget mount failed", err);
        renderFallbackMessage("Could not initialize the LI.FI widget. Please hard refresh and try again.");
    }
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
else mount();
