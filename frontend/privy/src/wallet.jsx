// GoodMarket — reusable Privy embedded-wallet controller (Phase 1: login).
//
// Mounts Privy headlessly and exposes an imperative bridge on `window.GMPrivy`
// so the existing (non-React) homepage JS can drive a "Create wallet powered by
// Privy" flow. The user logs in with email/Google, Privy auto-creates a
// self-custodial embedded wallet (no seed phrase), and we reuse GoodMarket's
// existing /verify-identity login flow to establish the session.
//
// Public API (window.GMPrivy):
//   isConfigured() -> bool
//   isReady()      -> bool        (PrivyProvider initialized)
//   isAuthenticated() -> bool
//   getAddress()   -> string|null (embedded wallet address)
//   getProvider()  -> Promise<EIP-1193 provider|null>  (used by Phase 2)
//   login()        -> Promise<{ address, provider }>   (opens modal if needed)
//   signMessage(message) -> Promise<string>            (EIP-191 personal_sign)
//   logout()       -> Promise<void>
//   onReady(cb)    -> void
//
// Phase 2 (universal signer): when the user logged in via Privy
// (localStorage.gm_login_method === "privy"), the embedded wallet's EIP-1193
// provider is installed as `window.ethereum` so the existing transaction code
// on feature pages (claim/swap/p2p/savings/send) signs through Privy with no
// extension and no per-action popup. Gated by the login method so injected /
// WalletConnect users are never affected. Pages can also opt in explicitly via
// GMPrivy.installAsDefaultSigner().

import React, { useEffect } from "react";
import { createRoot } from "react-dom/client";
import {
  PrivyProvider,
  usePrivy,
  useLogin,
  useWallets,
  useSignMessage,
} from "@privy-io/react-auth";
import { celo } from "viem/chains";

function readConfig() {
  const cfg = (typeof window !== "undefined" && window.__GM_PRIVY_CONFIG__) || {};
  return {
    appId: cfg.appId || "",
    rpcUrl: cfg.rpcUrl || "https://forno.celo.org",
    explorer: cfg.explorer || "https://celoscan.io",
  };
}

const CONFIG = readConfig();

// Module-level state for the imperative bridge between React and plain JS.
const state = {
  ready: false,
  authenticated: false,
  wallets: [],
  loginFn: null,
  signMessageFn: null,
  logoutFn: null,
  pendingLogin: null, // { resolve, reject }
  providerInstalled: false,
};

// Install the embedded wallet's EIP-1193 provider as the page's default signer.
// Most feature pages fall back to `window.ethereum` when WalletConnect is not
// connected, so this makes them transparently use Privy. Guarded against a
// non-writable `window.ethereum` (e.g. an injected extension locks it down).
function installProvider(provider) {
  if (!provider || typeof window === "undefined") return false;
  try {
    window.ethereum = provider;
  } catch (_) {
    try {
      Object.defineProperty(window, "ethereum", {
        value: provider,
        configurable: true,
        writable: true,
      });
    } catch (__) {
      /* couldn't override; still expose via the namespaced handle below */
    }
  }
  window.__GM_PRIVY_PROVIDER__ = provider;
  state.providerInstalled = true;
  try {
    const w = getEmbedded();
    window.dispatchEvent(
      new CustomEvent("gmprivy:provider", {
        detail: { address: w ? w.address : null },
      })
    );
  } catch (_) {
    /* ignore */
  }
  return true;
}

function shouldAutoInstallProvider() {
  try {
    return window.localStorage.getItem("gm_login_method") === "privy";
  } catch (_) {
    return false;
  }
}

async function maybeInstallProvider() {
  if (state.providerInstalled) return;
  if (!shouldAutoInstallProvider()) return;
  if (!state.authenticated) return;
  const wallet = getEmbedded();
  if (!wallet) return;
  try {
    const provider = await wallet.getEthereumProvider();
    installProvider(provider);
  } catch (_) {
    /* provider not ready yet; will retry on the next auth/wallets change */
  }
}

function getEmbedded() {
  return (
    state.wallets.find((w) => w.walletClientType === "privy") ||
    state.wallets[0] ||
    null
  );
}

async function resolvePendingLogin() {
  if (!state.pendingLogin) return;
  if (!state.authenticated) return;
  const wallet = getEmbedded();
  if (!wallet) return; // embedded wallet still being created — wait for next tick
  const { resolve } = state.pendingLogin;
  state.pendingLogin = null;
  let provider = null;
  try {
    provider = await wallet.getEthereumProvider();
  } catch (_) {
    /* provider optional for the login flow; signMessage works without it */
  }
  resolve({ address: wallet.address, provider });
}

function Controller() {
  const privy = usePrivy();
  const { wallets } = useWallets();
  const { signMessage } = useSignMessage();
  const { login } = useLogin({
    onComplete: ({ user, isNewUser }) => {
      // Belt-and-suspenders: resolve pending login here as well in case the
      // useEffect on privy.authenticated fires too late or not at all.
      resolvePendingLogin();
    },
    onError: (err) => {
      if (state.pendingLogin) {
        const { reject } = state.pendingLogin;
        state.pendingLogin = null;
        reject(new Error(typeof err === "string" ? err : "Privy login failed"));
      }
    },
  });

  // Keep the module-level bridge pointing at the latest hook values.
  state.authenticated = privy.authenticated;
  state.wallets = wallets;
  state.loginFn = login;
  state.signMessageFn = signMessage;
  state.logoutFn = privy.logout;

  useEffect(() => {
    state.ready = privy.ready;
    if (privy.ready && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("gmprivy:ready"));
    }
  }, [privy.ready]);

  useEffect(() => {
    resolvePendingLogin();
    maybeInstallProvider();
  }, [privy.authenticated, wallets]);

  return null;
}

const readyCallbacks = [];
window.GMPrivy = {
  isConfigured: () => Boolean(CONFIG.appId),
  isReady: () => state.ready,
  isAuthenticated: () => state.authenticated,
  getAddress: () => {
    const w = getEmbedded();
    return w ? w.address : null;
  },
  getProvider: async () => {
    const w = getEmbedded();
    return w ? await w.getEthereumProvider() : null;
  },
  login: () =>
    new Promise((resolve, reject) => {
      if (!CONFIG.appId) {
        reject(new Error("Privy is not configured (missing PRIVY_APP_ID)."));
        return;
      }
      const existing = getEmbedded();
      if (state.authenticated && existing) {
        existing
          .getEthereumProvider()
          .then((p) => resolve({ address: existing.address, provider: p }))
          .catch(() => resolve({ address: existing.address, provider: null }));
        return;
      }
      state.pendingLogin = { resolve, reject };

      function openModal() {
        if (!state.loginFn) {
          state.pendingLogin = null;
          reject(new Error("Privy SDK failed to initialize (login function unavailable)."));
          return;
        }
        try {
          state.loginFn();
        } catch (err) {
          state.pendingLogin = null;
          reject(err);
        }
      }

      // Gate on SDK readiness — calling login() before ready silently no-ops
      // and the modal never appears.
      if (state.ready) {
        openModal();
      } else {
        const readyTimeout = setTimeout(() => {
          window.removeEventListener("gmprivy:ready", onReady);
          state.pendingLogin = null;
          reject(new Error("Privy took too long to initialize. Please try again."));
        }, 15000);
        function onReady() {
          clearTimeout(readyTimeout);
          openModal();
        }
        window.addEventListener("gmprivy:ready", onReady, { once: true });
      }
    }),
  signMessage: async (message) => {
    if (!state.signMessageFn) throw new Error("Privy is not ready yet.");
    const { signature } = await state.signMessageFn({ message });
    return signature;
  },
  logout: async () => {
    if (state.logoutFn) await state.logoutFn();
  },
  isProviderInstalled: () => state.providerInstalled,
  installAsDefaultSigner: async () => {
    const wallet = getEmbedded();
    if (!wallet) return false;
    try {
      const provider = await wallet.getEthereumProvider();
      return installProvider(provider);
    } catch (_) {
      return false;
    }
  },
  onReady: (cb) => {
    if (typeof cb !== "function") return;
    if (state.ready) cb();
    else readyCallbacks.push(cb);
  },
};

window.addEventListener("gmprivy:ready", () => {
  while (readyCallbacks.length) {
    try {
      readyCallbacks.shift()();
    } catch (_) {
      /* ignore individual callback errors */
    }
  }
});

function mount() {
  if (!CONFIG.appId) {
    // Nothing to mount; window.GMPrivy.login() will reject with a clear error.
    return;
  }
  let el = document.getElementById("gmPrivyController");
  if (!el) {
    el = document.createElement("div");
    el.id = "gmPrivyController";
    el.style.display = "none";
    document.body.appendChild(el);
  }
  createRoot(el).render(
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
      <Controller />
    </PrivyProvider>
  );
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount);
} else {
  mount();
}
