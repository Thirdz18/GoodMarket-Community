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
  useExportWallet,
  useCreateWallet,
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

// Reject a promise if it doesn't settle in time so a hung Privy SDK call
// (common right after a brand-new embedded wallet is provisioned) surfaces a
// retryable error instead of leaving the UI stuck on a status message forever.
function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(label || "The wallet took too long to respond. Please try again.")),
      ms
    );
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Wait up to `ms` milliseconds for a condition to become true, checking every
// `interval` ms. Returns the last value returned by `check`.
function waitFor(check, ms, interval) {
  return new Promise((resolve) => {
    const result = check();
    if (result) { resolve(result); return; }
    const end = Date.now() + ms;
    const id = setInterval(() => {
      const r = check();
      if (r || Date.now() >= end) {
        clearInterval(id);
        resolve(r || null);
      }
    }, interval || 200);
  });
}

// Module-level state for the imperative bridge between React and plain JS.
const state = {
  ready: false,
  authenticated: false,
  wallets: [],
  loginFn: null,
  signMessageFn: null,
  exportWalletFn: null,
  logoutFn: null,
  createWalletFn: null,
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

  // Wait up to 12 s for an embedded wallet to appear in state.wallets.
  // Privy's createOnLogin populates wallets asynchronously after the
  // authenticated event fires, so we must not bail out immediately.
  let wallet = await waitFor(getEmbedded, 12000, 300);

  // If still no wallet after waiting (e.g. returning user whose wallet was
  // never created), explicitly call createWallet() and wait again.
  if (!wallet && state.createWalletFn) {
    try {
      await state.createWalletFn({ chainType: "ethereum" });
    } catch (createErr) {
      const errMsg = (createErr && createErr.message) || "";
      // "already has wallet" is fine — it means the wallet exists but
      // hasn't propagated to state.wallets yet; keep waiting below.
      if (!/already/i.test(errMsg)) {
        if (state.pendingLogin) {
          const { reject } = state.pendingLogin;
          state.pendingLogin = null;
          reject(new Error("Could not create your wallet. Please try again."));
        }
        return;
      }
    }
    // Wait a second time for the new wallet to appear in React state.
    wallet = await waitFor(getEmbedded, 10000, 300);
  }

  if (!wallet) {
    // Wallet creation succeeded on Privy's side but React state never
    // updated — this is an edge case; reject so the user sees a message.
    if (state.pendingLogin) {
      const { reject } = state.pendingLogin;
      state.pendingLogin = null;
      reject(new Error("Wallet is taking too long to initialize. Please refresh and try again."));
    }
    return;
  }

  if (!state.pendingLogin) return; // may have been cancelled while we waited
  const { resolve } = state.pendingLogin;
  state.pendingLogin = null;
  // provider is optional for signing — signMessage goes through Privy directly
  resolve({ address: wallet.address, provider: null });
}

function Controller() {
  const privy = usePrivy();
  const { wallets } = useWallets();
  const { signMessage } = useSignMessage();
  const { exportWallet } = useExportWallet();
  const { createWallet } = useCreateWallet();
  const { login } = useLogin({
    onComplete: () => {
      // Fire-and-forget but must be async so createWallet() inside is awaited.
      resolvePendingLogin().catch((err) => {
        if (state.pendingLogin) {
          const { reject } = state.pendingLogin;
          state.pendingLogin = null;
          reject(err);
        }
      });
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
  state.exportWalletFn = exportWallet;
  state.createWalletFn = createWallet;
  state.logoutFn = privy.logout;

  useEffect(() => {
    state.ready = privy.ready;
    if (privy.ready && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("gmprivy:ready"));
    }
  }, [privy.ready]);

  useEffect(() => {
    // Only auto-install the provider on return visits (gm_login_method=privy).
    // Do NOT call resolvePendingLogin() here — onComplete handles it.
    // Calling it from the effect as well causes double createWallet() calls
    // when wallets array updates right after onComplete fires.
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
          // Explicitly pass loginMethods so the Privy modal always shows
          // the social-login options (email / Google) instead of falling
          // back to dashboard defaults which may auto-connect a browser
          // wallet or skip the chooser entirely.
          state.loginFn({ loginMethods: ["email", "google"] });
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
    // A freshly created embedded wallet can lag a few hundred ms behind the
    // "authenticated" event: the signer function and/or the wallet object may
    // not exist for the first moment after login. Wait for both to be ready
    // (bounded) instead of throwing immediately.
    const deadline = Date.now() + 12000;
    while ((!state.signMessageFn || !getEmbedded()) && Date.now() < deadline) {
      await sleep(200);
    }
    if (!state.signMessageFn || !getEmbedded()) {
      throw new Error("Wallet is still being set up. Please try again in a moment.");
    }

    // Retry transient "not ready / not connected" signer failures that happen
    // right after wallet creation. Never retry an explicit user rejection.
    let lastErr = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const { signature } = await withTimeout(
          state.signMessageFn({ message }),
          15000,
          "Signing timed out. Please try again."
        );
        if (signature) return signature;
        lastErr = new Error("The wallet returned an empty signature. Please try again.");
      } catch (err) {
        lastErr = err;
        const msg = (err && err.message) || "";
        if (/reject|denied|cancel|user closed|exit/i.test(msg)) throw err;
      }
      if (attempt < 2) await sleep(700);
    }
    throw lastErr || new Error("Unable to sign the login message. Please try again.");
  },
  exportWallet: async () => {
    if (!state.exportWalletFn) throw new Error("Privy is not ready yet.");
    if (!state.authenticated) throw new Error("You must be logged in to export your wallet.");
    if (!getEmbedded()) throw new Error("No embedded wallet found.");
    await state.exportWalletFn();
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
    // Do NOT use display:none — Privy's SDK renders internal iframes and
    // dialog overlays as children of PrivyProvider. Hiding the container
    // prevents those iframes from loading, which breaks the login modal.
    // The element is empty (Controller returns null) and takes no visual
    // space; Privy's own modal uses fixed positioning with a high z-index.
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
