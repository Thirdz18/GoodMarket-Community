/**
 * GoodMarket — LI.FI / Jumper widget mount
 * ---------------------------------------------------------------------------
 * Renders the @lifi/widget React component into #lifiWidgetRoot on the
 * /swap "Buy Crypto" tab.  LI.FI's widget handles its own wallet connection
 * (injected EIP-1193 wallets + WalletConnect via wagmi), so users can
 * connect from inside the widget regardless of how they signed in to
 * GoodMarket itself.  The widget supports cross-chain swaps between many
 * chains; defaults target Celo → native ETH on Base but the user can
 * use one of GoodMarket's curated Celo → Base routes.
 *
 * The widget is loaded via esm.sh on demand to keep the rest of /swap
 * snappy — it is only fetched/mounted when this script runs (and the
 * inline page script defers the script tag until the user opens the
 * Buy Crypto tab).
 *
 * Exposes window.GMLifiReactWidget = { refresh() } so the host page can
 * re-render the widget after layout changes (e.g. tab switching).
 */
import React, { useMemo } from "https://esm.sh/react@18.3.1";
import { createRoot } from "https://esm.sh/react-dom@18.3.1/client";
import { LiFiWidget } from "https://esm.sh/@lifi/widget@3?deps=react@18.3.1,react-dom@18.3.1";

const NATIVE_TOKEN = "0x0000000000000000000000000000000000000000";
const CELO_CHAIN_ID = 42220;
const BASE_CHAIN_ID = 8453;
const CELO_CUSD = "0x765DE816845861e75A25fCA122bb6898B8B1282a";
const CELO_USDC = "0xcebA9300f2b948710d2653dD7B07f33A8B32118C";
const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

const CURATED_LIFI_ROUTES = [
    {
        id: "usdc-base-usdc",
        priority: 3,
        title: "USDC → Base USDC",
        subtitle: "Best default for marketplace funds",
        badge: "Recommended",
        fromChain: CELO_CHAIN_ID,
        toChain: BASE_CHAIN_ID,
        fromToken: CELO_USDC,
        toToken: BASE_USDC,
    },
    {
        id: "celo-base-usdc",
        priority: 4,
        title: "CELO → Base USDC",
        subtitle: "Use CELO balance, receive stable USDC",
        badge: "Recommended",
        fromChain: CELO_CHAIN_ID,
        toChain: BASE_CHAIN_ID,
        fromToken: NATIVE_TOKEN,
        toToken: BASE_USDC,
    },
    {
        id: "cusd-base-usdc",
        priority: 5,
        title: "cUSD → Base USDC",
        subtitle: "Show only if LI.FI quote succeeds",
        badge: "Quote-gated",
        fromChain: CELO_CHAIN_ID,
        toChain: BASE_CHAIN_ID,
        fromToken: CELO_CUSD,
        toToken: BASE_USDC,
    },
    {
        id: "celo-base-eth",
        priority: 6,
        title: "CELO → Base ETH",
        subtitle: "For Base gas funding",
        badge: "Gas",
        fromChain: CELO_CHAIN_ID,
        toChain: BASE_CHAIN_ID,
        fromToken: NATIVE_TOKEN,
        toToken: NATIVE_TOKEN,
    },
    {
        id: "usdc-base-eth",
        priority: 7,
        title: "USDC → Base ETH",
        subtitle: "Optional gas route",
        badge: "Optional",
        fromChain: CELO_CHAIN_ID,
        toChain: BASE_CHAIN_ID,
        fromToken: CELO_USDC,
        toToken: NATIVE_TOKEN,
    },
];

function tokenRef(chainId, address) {
    return { chainId, address };
}

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

function renderFallbackMessage(message) {
    const rootEl = document.getElementById("lifiWidgetRoot");
    if (!rootEl) return;
    rootEl.innerHTML = "";
    const box = document.createElement("div");
    box.className = "lifi-react-status";
    box.setAttribute("data-connected", "false");
    box.style.marginBottom = "0.75rem";
    box.innerHTML = `<strong>⚠️ Buy Crypto widget unavailable</strong><span>${message}</span>`;
    rootEl.appendChild(box);
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

function makeConfig(bootstrap, route = CURATED_LIFI_ROUTES[0]) {
    const integrator = bootstrap.integrator || "goodmarket-community";
    const fromChain = Number(route?.fromChain || bootstrap.fromChainId || CELO_CHAIN_ID);
    const toChain = Number(route?.toChain || bootstrap.toChainId || BASE_CHAIN_ID);
    const fromToken = route?.fromToken || bootstrap.fromToken || NATIVE_TOKEN;
    const toToken = route?.toToken || bootstrap.toToken || NATIVE_TOKEN;

    const config = {
        integrator,
        variant: "compact",
        appearance: "dark",
        fromChain,
        toChain,
        fromToken,
        toToken,
        chains: {
            allow: Array.from(new Set([fromChain, toChain])),
        },
        tokens: {
            from: { allow: [tokenRef(fromChain, fromToken)] },
            to: { allow: [tokenRef(toChain, toToken)] },
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

    if (bootstrap.apiUrl) {
        config.sdkConfig = { apiUrl: bootstrap.apiUrl };
    }

    if (isPresentWallet(bootstrap.walletAddress)) {
        config.toAddress = {
            address: bootstrap.walletAddress,
            chainType: "EVM",
        };
    }

    return config;
}

function CuratedRoutePicker({ routes, selectedId, onSelect }) {
    return React.createElement("div", { className: "lifi-route-picker", "aria-label": "GoodMarket curated LI.FI routes" },
        React.createElement("div", { className: "lifi-route-picker-head" },
            React.createElement("strong", null, "GoodMarket supported LI.FI routes"),
            React.createElement("span", null, "Only curated Celo → Base routes are shown here. USDT routes are hidden from the main UX.")
        ),
        React.createElement("div", { className: "lifi-route-grid" },
            routes.map((route) => React.createElement("button", {
                key: route.id,
                type: "button",
                className: `lifi-route-card${route.id === selectedId ? " is-active" : ""}`,
                onClick: () => onSelect(route.id),
                "aria-pressed": route.id === selectedId ? "true" : "false",
            },
                React.createElement("span", { className: "lifi-route-priority" }, `#${route.priority}`),
                React.createElement("span", { className: "lifi-route-main" },
                    React.createElement("strong", null, route.title),
                    React.createElement("small", null, route.subtitle)
                ),
                React.createElement("span", { className: "lifi-route-badge" }, route.badge)
            ))
        )
    );
}

function GoodMarketLifiWidget({ bootstrap }) {
    const [selectedRouteId, setSelectedRouteId] = React.useState(CURATED_LIFI_ROUTES[0].id);
    const selectedRoute = CURATED_LIFI_ROUTES.find((route) => route.id === selectedRouteId) || CURATED_LIFI_ROUTES[0];
    const config = useMemo(() => makeConfig(bootstrap, selectedRoute), [bootstrap, selectedRoute]);

    return React.createElement(React.Fragment, null,
        React.createElement(CuratedRoutePicker, {
            routes: CURATED_LIFI_ROUTES,
            selectedId: selectedRoute.id,
            onSelect: setSelectedRouteId,
        }),
        React.createElement(LiFiWidget, {
            key: selectedRoute.id,
            integrator: config.integrator,
            config,
        })
    );
}

let _root = null;

function mount() {
    const rootEl = document.getElementById("lifiWidgetRoot");
    if (!rootEl) return;
    const bootstrap = readBootstrap();
    try {
        if (!_root) _root = createRoot(rootEl);
        _root.render(
            React.createElement(WidgetErrorBoundary, null,
                React.createElement(GoodMarketLifiWidget, { bootstrap })
            )
        );
        window.GMLifiReactWidget = {
            refresh: () => {
                try { mount(); } catch (err) {
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
