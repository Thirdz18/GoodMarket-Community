import React, { useEffect, useMemo, useState } from "https://esm.sh/react@18.3.1";
import { createRoot } from "https://esm.sh/react-dom@18.3.1/client";
import { SquidWidget } from "https://esm.sh/@0xsquid/widget@6.0.1?deps=react@18.3.1,react-dom@18.3.1";

const NATIVE_TOKEN = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE";
const CELO_HEX_CHAIN_ID = "0xa4ec";
const CELO_CHAIN_PARAMS = {
    chainId: CELO_HEX_CHAIN_ID,
    chainName: "Celo Mainnet",
    nativeCurrency: { name: "CELO", symbol: "CELO", decimals: 18 },
    rpcUrls: ["https://forno.celo.org"],
    blockExplorerUrls: ["https://celoscan.io"],
};

function renderFallbackMessage(message) {
    const rootEl = document.getElementById("squidReactWidgetRoot");
    if (!rootEl) return;
    rootEl.innerHTML = "";
    const box = document.createElement("div");
    box.className = "squid-react-status";
    box.setAttribute("data-connected", "false");
    box.style.marginBottom = "0.75rem";
    box.innerHTML = `<strong>⚠️ Buy ETH widget unavailable</strong><span>${message}</span>`;
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
        console.error("[GoodMarket Squid] React widget render failed", error);
    }
    render() {
        if (this.state.hasError) {
            return React.createElement("div", { className: "squid-react-status", "data-connected": "false" },
                React.createElement("strong", null, "⚠️ Buy ETH widget failed to render"),
                React.createElement("span", null, "Please refresh the page. If this continues, reconnect your wallet and try again.")
            );
        }
        return this.props.children;
    }
}

function readBootstrap() {
    const node = document.getElementById("squidWidgetBootstrap");
    if (!node) return {};
    try { return JSON.parse(node.textContent || "{}"); }
    catch (err) {
        console.error("[GoodMarket Squid] Invalid widget bootstrap JSON", err);
        return {};
    }
}

function isPresentWallet(address) {
    return Boolean(address && address !== "None" && /^0x[0-9a-fA-F]{40}$/.test(address));
}

function shortAddress(address) {
    return isPresentWallet(address) ? `${address.slice(0, 6)}…${address.slice(-4)}` : "Not connected";
}

function normalizeToken(token) {
    return {
        symbol: token?.symbol || "CELO",
        address: token?.address || NATIVE_TOKEN,
    };
}

function makeConfig(bootstrap, sourceToken) {
    const baseConfig = bootstrap.widgetConfig || {};
    const sourceChainId = Number(bootstrap.fromChainId || 42220);
    const destinationChainId = Number(bootstrap.toChainId || 8453);
    const token = normalizeToken(sourceToken || bootstrap.sourceTokens?.[0]);
    const config = {
        ...baseConfig,
        apiUrl: bootstrap.apiUrl || baseConfig.apiUrl || "https://v2.api.squidrouter.com",
        themeType: baseConfig.themeType || "dark",
        initialAssets: {
            ...(baseConfig.initialAssets || {}),
            from: { address: token.address, chainId: Number.isFinite(sourceChainId) ? sourceChainId : 42220 },
            to: { address: bootstrap.toToken || NATIVE_TOKEN, chainId: Number.isFinite(destinationChainId) ? destinationChainId : 8453 },
        },
    };
    // Squid's SquidProvider throws (and renders blank) when integratorId is
    // empty, and its API rejects unregistered IDs with 401. We therefore
    // always send an integratorId, defaulting to Squid's public
    // "squid-swap-widget" id, which authenticates without contacting Squid
    // support and needs no env var. Operators can still override it via the
    // backend's SQUID_INTEGRATOR_ID env var if they register their own.
    config.integratorId = bootstrap.integratorId || baseConfig.integratorId || "squid-swap-widget";
    if (isPresentWallet(bootstrap.walletAddress)) {
        config.initialRecipientAddress = bootstrap.walletAddress;
        config.toAddress = bootstrap.walletAddress;
    }
    return config;
}

function emitProvider(provider) {
    if (!provider) return;
    const detail = {
        info: {
            uuid: "676f6f64-6d61-726b-6574-737175696400",
            name: "GoodMarket Wallet",
            icon: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' fill='%237c3aed'/%3E%3Ctext x='32' y='39' text-anchor='middle' font-size='24' font-family='Arial' fill='white'%3EGM%3C/text%3E%3C/svg%3E",
            rdns: "community.goodmarket.wallet",
        },
        provider,
    };
    const announce = () => window.dispatchEvent(new CustomEvent("eip6963:announceProvider", { detail }));
    window.removeEventListener("eip6963:requestProvider", announce);
    window.addEventListener("eip6963:requestProvider", announce);
    announce();
}

async function requestProviderAccounts(provider) {
    if (!provider?.request) return [];
    try {
        const accounts = await provider.request({ method: "eth_accounts" });
        if (Array.isArray(accounts) && accounts.length) return accounts;
    } catch (_) {}
    try {
        const accounts = await provider.request({ method: "eth_requestAccounts" });
        return Array.isArray(accounts) ? accounts : [];
    } catch (err) {
        console.warn("[GoodMarket Squid] Provider account request skipped/failed", err);
        return [];
    }
}

async function switchToCelo(provider) {
    if (!provider?.request) return;
    try {
        const chainId = await provider.request({ method: "eth_chainId" });
        if (String(chainId).toLowerCase() === CELO_HEX_CHAIN_ID) return;
    } catch (_) {}
    try {
        await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CELO_HEX_CHAIN_ID }] });
    } catch (err) {
        if (err?.code === 4902) {
            try { await provider.request({ method: "wallet_addEthereumChain", params: [CELO_CHAIN_PARAMS] }); }
            catch (addErr) { console.warn("[GoodMarket Squid] Celo chain add failed", addErr); }
        } else {
            console.warn("[GoodMarket Squid] Celo chain switch skipped/failed", err);
        }
    }
}

async function resolveGoodMarketProvider() {
    if (typeof window.GMSquidResolveProvider === "function") {
        try {
            const provider = await window.GMSquidResolveProvider();
            if (provider) return provider;
        } catch (err) {
            console.warn("[GoodMarket Squid] GoodMarket provider resolver failed", err);
        }
    }
    if (window.ethereum) return window.ethereum;
    if (window.GMWalletConnect?.getProvider && window.GMWalletConnect.isPreferred?.()) {
        try { return await window.GMWalletConnect.getProvider(); }
        catch (err) { console.warn("[GoodMarket Squid] WalletConnect provider failed", err); }
    }
    return null;
}

function SquidGoodMarketWidget({ bootstrap }) {
    const [sourceToken, setSourceToken] = useState(normalizeToken(bootstrap.sourceTokens?.[0]));
    const [provider, setProvider] = useState(null);
    const [connectedAddress, setConnectedAddress] = useState(bootstrap.walletAddress || "");
    const [status, setStatus] = useState("Connecting GoodMarket wallet…");

    useEffect(() => {
        let cancelled = false;
        window.GMSquidReactWidget = {
            setSourceToken: (symbol, address) => setSourceToken(normalizeToken({ symbol, address })),
            refresh: () => setSourceToken((current) => ({ ...current })),
        };
        (async () => {
            const nextProvider = await resolveGoodMarketProvider();
            if (cancelled) return;
            if (!nextProvider) {
                setStatus("No GoodMarket wallet provider detected yet. Connect your wallet above, then reopen this tab.");
                return;
            }
            emitProvider(nextProvider);
            if (!window.ethereum) window.ethereum = nextProvider;
            setProvider(nextProvider);
            const accounts = await requestProviderAccounts(nextProvider);
            if (cancelled) return;
            const account = accounts?.[0] || bootstrap.walletAddress || "";
            if (account) setConnectedAddress(account);
            setStatus(account ? `Auto-connected ${shortAddress(account)} to the Squid widget.` : "Provider ready; open the widget wallet menu if it asks for confirmation.");
            await switchToCelo(nextProvider);
        })();
        return () => { cancelled = true; };
    }, [bootstrap]);

    const widgetConfig = useMemo(() => makeConfig(bootstrap, sourceToken), [bootstrap, sourceToken]);
    const widgetKey = `${sourceToken.symbol}:${sourceToken.address}:${connectedAddress || "anon"}`;

    return React.createElement(React.Fragment, null,
        React.createElement("div", { className: "squid-react-status", "data-connected": provider ? "true" : "false" },
            React.createElement("strong", null, provider ? "✅ In-widget wallet bridge" : "🔌 In-widget wallet bridge"),
            React.createElement("span", null, status)
        ),
        React.createElement("div", { className: "squid-react-shell" },
            React.createElement(SquidWidget, {
                key: widgetKey,
                config: widgetConfig,
                provider,
                evmProvider: provider,
                walletProvider: provider,
                externalProvider: provider,
                initialSignerAddress: isPresentWallet(connectedAddress) ? connectedAddress : undefined,
            })
        )
    );
}

function mount() {
    const rootEl = document.getElementById("squidReactWidgetRoot");
    if (!rootEl) return;
    try {
        const root = createRoot(rootEl);
        root.render(
            React.createElement(WidgetErrorBoundary, null,
                React.createElement(SquidGoodMarketWidget, { bootstrap: readBootstrap() })
            )
        );
    } catch (err) {
        console.error("[GoodMarket Squid] Widget mount failed", err);
        renderFallbackMessage("Could not initialize the Squid integration script. Please hard refresh and try again.");
    }
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
else mount();
