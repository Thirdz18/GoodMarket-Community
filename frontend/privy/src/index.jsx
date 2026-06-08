// GoodMarket — Privy embedded-wallet proof of concept.
//
// Demonstrates a "MiniPay-style" experience: the user logs in with email /
// Google (no seed phrase, no "Connect MetaMask"), Privy auto-creates a
// self-custodial embedded wallet, and the user can sign messages / send
// transactions WITHOUT a MetaMask-style popup for every action.
//
// The bundle reads its config from window.__GM_PRIVY_CONFIG__ which is injected
// by templates/embedded_wallet.html (server-rendered, so the Privy App ID and
// chain settings stay configurable from Flask).

import React, { useMemo, useState, useEffect, useCallback } from "react";
import { createRoot } from "react-dom/client";
import {
  PrivyProvider,
  usePrivy,
  useWallets,
  useLogin,
  useSignMessage,
  useSendTransaction,
} from "@privy-io/react-auth";
import { celo } from "viem/chains";
import { createPublicClient, http, formatEther } from "viem";

function readConfig() {
  const cfg = (typeof window !== "undefined" && window.__GM_PRIVY_CONFIG__) || {};
  return {
    appId: cfg.appId || "",
    rpcUrl: cfg.rpcUrl || "https://forno.celo.org",
    explorer: cfg.explorer || "https://celoscan.io",
  };
}

const CONFIG = readConfig();

const publicClient = createPublicClient({
  chain: celo,
  transport: http(CONFIG.rpcUrl),
});

function short(addr) {
  if (!addr) return "";
  return addr.slice(0, 6) + "…" + addr.slice(-4);
}

function Panel({ children }) {
  return <div className="gm-privy-panel">{children}</div>;
}

function Row({ label, children }) {
  return (
    <div className="gm-privy-row">
      <span className="gm-privy-row-label">{label}</span>
      <span className="gm-privy-row-value">{children}</span>
    </div>
  );
}

function WalletDemo() {
  const { ready, authenticated, user, logout } = usePrivy();
  const { login } = useLogin();
  const { wallets } = useWallets();
  const { signMessage } = useSignMessage();
  const { sendTransaction } = useSendTransaction();

  const [balance, setBalance] = useState(null);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState([]);

  const embedded = useMemo(
    () => wallets.find((w) => w.walletClientType === "privy") || wallets[0],
    [wallets]
  );
  const address = embedded?.address || user?.wallet?.address || "";

  const pushLog = useCallback((msg) => {
    setLog((prev) => [{ t: Date.now(), msg }, ...prev].slice(0, 8));
  }, []);

  const refreshBalance = useCallback(async () => {
    if (!address) return;
    try {
      const wei = await publicClient.getBalance({ address });
      setBalance(formatEther(wei));
    } catch (err) {
      pushLog("Balance error: " + (err?.message || err));
    }
  }, [address, pushLog]);

  useEffect(() => {
    refreshBalance();
  }, [refreshBalance]);

  const onSign = useCallback(async () => {
    if (!address) return;
    setBusy(true);
    try {
      const { signature } = await signMessage({
        message: "GoodMarket embedded wallet — gasless signing demo",
      });
      pushLog("Signed (no popup): " + signature.slice(0, 18) + "…");
    } catch (err) {
      pushLog("Sign error: " + (err?.message || err));
    } finally {
      setBusy(false);
    }
  }, [address, signMessage, pushLog]);

  const onSend = useCallback(async () => {
    if (!address) return;
    setBusy(true);
    try {
      // Self-transfer of 0 CELO — proves transaction signing/broadcast works
      // through the embedded wallet without a MetaMask-style approval popup.
      const { hash } = await sendTransaction({
        to: address,
        value: 0,
        chainId: celo.id,
      });
      pushLog("Sent tx: " + hash.slice(0, 18) + "…");
      setTimeout(refreshBalance, 4000);
    } catch (err) {
      pushLog("Send error: " + (err?.message || err));
    } finally {
      setBusy(false);
    }
  }, [address, sendTransaction, refreshBalance, pushLog]);

  if (!ready) {
    return (
      <Panel>
        <p className="gm-privy-muted">Loading Privy…</p>
      </Panel>
    );
  }

  if (!authenticated) {
    return (
      <Panel>
        <h2 className="gm-privy-title">Create your GoodMarket wallet</h2>
        <p className="gm-privy-muted">
          Mag-login gamit ang email o Google — automatic na gagawa ng sarili
          mong wallet. Walang seed phrase, walang browser extension.
        </p>
        <button className="gm-privy-btn gm-privy-btn-primary" onClick={login}>
          Mag-login / Gumawa ng wallet
        </button>
      </Panel>
    );
  }

  return (
    <Panel>
      <h2 className="gm-privy-title">Your embedded wallet</h2>
      <Row label="Status">
        <span className="gm-privy-pill">Connected</span>
      </Row>
      <Row label="Login">{user?.email?.address || user?.google?.email || user?.id}</Row>
      <Row label="Address">
        {address ? (
          <a
            className="gm-privy-link"
            href={`${CONFIG.explorer}/address/${address}`}
            target="_blank"
            rel="noreferrer"
          >
            {short(address)}
          </a>
        ) : (
          "creating…"
        )}
      </Row>
      <Row label="Balance">
        {balance === null ? "…" : `${Number(balance).toFixed(4)} CELO`}
      </Row>

      <div className="gm-privy-actions">
        <button
          className="gm-privy-btn"
          onClick={onSign}
          disabled={busy || !address}
        >
          Sign message (no popup)
        </button>
        <button
          className="gm-privy-btn"
          onClick={onSend}
          disabled={busy || !address}
        >
          Send 0 CELO test tx
        </button>
        <button className="gm-privy-btn" onClick={refreshBalance} disabled={busy}>
          Refresh balance
        </button>
      </div>

      <div className="gm-privy-log">
        {log.length === 0 ? (
          <span className="gm-privy-muted">No activity yet.</span>
        ) : (
          log.map((l) => (
            <div key={l.t} className="gm-privy-log-line">
              {l.msg}
            </div>
          ))
        )}
      </div>

      <button className="gm-privy-btn gm-privy-btn-ghost" onClick={logout}>
        Logout
      </button>
    </Panel>
  );
}

function App() {
  if (!CONFIG.appId) {
    return (
      <Panel>
        <h2 className="gm-privy-title">Privy not configured</h2>
        <p className="gm-privy-muted">
          Missing Privy App ID. Set <code>PRIVY_APP_ID</code> in the server
          environment.
        </p>
      </Panel>
    );
  }
  return (
    <PrivyProvider
      appId={CONFIG.appId}
      config={{
        appearance: { theme: "dark", accentColor: "#5cc8ff" },
        loginMethods: ["email", "google"],
        embeddedWallets: {
          ethereum: { createOnLogin: "users-without-wallets" },
          showWalletUIs: false,
        },
        defaultChain: celo,
        supportedChains: [celo],
      }}
    >
      <WalletDemo />
    </PrivyProvider>
  );
}

function mount() {
  const el = document.getElementById("gmPrivyRoot");
  if (!el) return;
  createRoot(el).render(<App />);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount);
} else {
  mount();
}
