/**
 * Wallet-signing helpers for the trustless P2P escrow flow.
 *
 * The backend (p2p_trading/escrow_service.py) builds unsigned transactions
 * and returns them to the browser; this module forwards them to the user's
 * injected wallet (window.ethereum / MiniPay) for signing.
 *
 * No private key handling here — the user signs every transaction in their
 * wallet UI.
 */
(function () {
  "use strict";

  const CHAIN_ID_CELO_MAINNET = 42220;
  const CHAIN_ID_HEX = "0x" + CHAIN_ID_CELO_MAINNET.toString(16);
  const DEFAULT_CONFIG = {
    walletAddress: "",
    loginMethod: "",
    projectId: "",
    dappName: "GoodMarket — P2P",
    dappDescription: "Trustless peer-to-peer escrow trading on Celo",
    assetVersion: "",
    sidecarEnabled: true,
  };
  const _config = { ...DEFAULT_CONFIG };

  // Celo RPC fallback URLs for reliability
  const CELO_RPC_URLS = [
    "https://forno.celo.org",
    "https://1rpc.io/celo",
    "https://celo.publicnode.com"
  ];

  function _normLogin(loginMethod) {
    return String(loginMethod || "").trim().toLowerCase();
  }

  function _shouldPreferWalletConnect() {
    return ["walletconnect", "manual", "manual_address"].includes(_normLogin(_config.loginMethod));
  }

  function configure(opts) {
    if (!opts || typeof opts !== "object") return;
    Object.assign(_config, opts);
  }

  /** Return the injected provider (MetaMask / MiniPay / Valora) or null. */
  function getInjectedProvider() {
    if (typeof window === "undefined") return null;
    if (window.ethereum && window.ethereum.providers && window.ethereum.providers.length) {
      // Prefer MiniPay if present so Celo dapp users get the integrated flow.
      const mp = window.ethereum.providers.find((p) => p && p.isMiniPay);
      if (mp) return mp;
      const mm = window.ethereum.providers.find((p) => p && p.isMetaMask);
      return mm || window.ethereum.providers[0];
    }
    return window.ethereum || null;
  }

  async function getSigningProvider() {
    const injected = getInjectedProvider();
    if (injected) return injected;
    if (_shouldPreferWalletConnect() && typeof GMWalletConnect !== "undefined") {
      try {
        const wcProvider = await GMWalletConnect.getProvider();
        if (wcProvider) return wcProvider;
      } catch (_) {}
    }
    return null;
  }

  async function ensureCeloChain(provider) {
    try {
      const cur = await provider.request({ method: "eth_chainId" });
      if (cur && cur.toLowerCase() === CHAIN_ID_HEX) return;
    } catch (_) { /* fall through */ }

    try {
      await provider.request({
        method: "wallet_switchEthereumChain",
        params: [{ chainId: CHAIN_ID_HEX }],
      });
      return;
    } catch (err) {
      if (err && err.code === 4902) {
        await provider.request({
          method: "wallet_addEthereumChain",
          params: [{
            chainId: CHAIN_ID_HEX,
            chainName: "Celo",
            nativeCurrency: { name: "CELO", symbol: "CELO", decimals: 18 },
            rpcUrls: CELO_RPC_URLS,
            blockExplorerUrls: ["https://celoscan.io"],
          }],
        });
        return;
      }
      throw err;
    }
  }

  async function requestAccount(provider) {
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    if (!accounts || !accounts.length) {
      throw new Error("No wallet account available");
    }
    return accounts[0];
  }

  /**
   * Sign and broadcast a single transaction prepared by the backend.
   * Returns the tx hash. Throws on user rejection / wallet error.
   */
  async function sendPreparedTx(prepared) {
    const provider = await getSigningProvider();
    if (!provider) {
      throw new Error("No wallet detected. Open in MiniPay, MetaMask, or Valora.");
    }
    await ensureCeloChain(provider);
    const from = (await requestAccount(provider)).toLowerCase();
    if (prepared.from && prepared.from.toLowerCase() !== from) {
      throw new Error(
        "Wallet account does not match the session. Switch to " +
        prepared.from.slice(0, 6) + "..." + prepared.from.slice(-4)
      );
    }

    const txParams = {
      from,
      to: prepared.to,
      data: prepared.data,
      value: prepared.value || "0x0",
    };
    if (prepared.gas) txParams.gas = prepared.gas;

    return provider.request({
      method: "eth_sendTransaction",
      params: [txParams],
    });
  }

  /**
   * Poll eth_getTransactionReceipt until the tx is mined, then return the
   * receipt. Throws on revert (status !== "0x1") or after `timeoutMs`.
   * Used between two dependent transactions (e.g. approve → openAd) so we
   * don't queue the second tx if the first one reverts.
   */
  async function waitForReceipt(txHash, opts) {
    const provider = await getSigningProvider();
    if (!provider) throw new Error("No wallet detected.");
    const intervalMs = (opts && opts.intervalMs) || 3000;
    const timeoutMs = (opts && opts.timeoutMs) || 120000;
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      let receipt = null;
      try {
        receipt = await provider.request({
          method: "eth_getTransactionReceipt",
          params: [txHash],
        });
      } catch (_) { /* keep polling */ }
      if (receipt && receipt.blockNumber) {
        if (receipt.status && receipt.status !== "0x1") {
          throw new Error("Transaction reverted: " + txHash);
        }
        return receipt;
      }
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    throw new Error("Timed out waiting for tx confirmation: " + txHash);
  }

  /** Notify the backend that a tx was submitted, so it can show "pending". */
  async function reportSubmitted(kind, identifier, txHash) {
    try {
      await fetch("/p2p/api/tx-submitted", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, identifier, tx_hash: txHash }),
      });
    } catch (_) { /* best-effort */ }
  }

  /** Convenience wrapper: hit a JSON endpoint and unwrap {success,...}. */
  async function jsonPost(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || !data.success) {
      const err = new Error(data.error || ("HTTP " + res.status));
      err.payload = data;
      throw err;
    }
    return data;
  }

  async function jsonGet(url) {
    const res = await fetch(url);
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || !data.success) {
      const err = new Error(data.error || ("HTTP " + res.status));
      err.payload = data;
      throw err;
    }
    return data;
  }

  // ----- High-level operations exposed to the page -----

  /** Open an ad: optionally approve G$ first, then call openAd. */
  async function openAd(adInputs) {
    const prep = await jsonPost("/p2p/api/ads/prepare-open", adInputs);
    const order = prep.order;
    const txs = prep.transactions || {};
    const txHashes = {};

    if (prep.approve_needed && txs.approve) {
      txHashes.approve = await sendPreparedTx(txs.approve);
      // Wait for approve to be mined before sending openAd. Otherwise a
      // reverted approve (e.g. token requires reset-to-zero, or wallet
      // overrode the gas limit) would queue a second tx that's guaranteed
      // to fail and burn gas.
      await waitForReceipt(txHashes.approve);
    }
    if (txs.open_ad) {
      txHashes.open_ad = await sendPreparedTx(txs.open_ad);
      // Report submitted - NON-BLOCKING (best effort)
      // Even if this fails, the ad should still appear
      reportSubmitted("ad", order.order_id, txHashes.open_ad).catch(err => {
        console.warn("reportSubmitted (ad) failed (non-critical):", err);
      });
    }
    return { order, txHashes };
  }

  async function placeOrder(orderId, amountGd, paymentWindowSeconds) {
    const prep = await jsonPost(
      "/p2p/api/orders/" + encodeURIComponent(orderId) + "/prepare-place",
      {
        amount_g_dollar: amountGd,
        payment_window_seconds: paymentWindowSeconds || null,
      }
    );
    const trade = prep.trade;
    const tx = (prep.transactions || {}).place_order;
    const txHash = await sendPreparedTx(tx);
    
    // Report submitted - NON-BLOCKING (best effort)
    // Even if this fails, the trade should still appear
    reportSubmitted("trade", trade.trade_id, txHash).catch(err => {
      console.warn("reportSubmitted failed (non-critical):", err);
    });
    
    return { trade, txHash };
  }

  async function uploadProof(tradeId, proofUrl) {
    return jsonPost(
      "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/upload-proof",
      { proof_url: proofUrl }
    );
  }

  /**
   * Upload a binary file (image / PDF) as a payment proof.
   * The backend stores it in a private Supabase Storage bucket and returns
   * the new proof's metadata + view URL.
   */
  async function uploadProofFile(tradeId, file) {
    if (!file) throw new Error("No file selected");
    const form = new FormData();
    form.append("file", file, file.name || "proof");
    const res = await fetch(
      "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/proof-upload",
      { method: "POST", body: form }
    );
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || !data.success) {
      const err = new Error(data.error || ("HTTP " + res.status));
      err.data = data;
      throw err;
    }
    return data;
  }

  function listProofs(tradeId) {
    return jsonGet(
      "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/proofs"
    );
  }

  // ---------- chat ----------

  function listChat(tradeId, opts) {
    const since = (opts && opts.since) ? "?since=" + encodeURIComponent(opts.since) : "";
    return jsonGet(
      "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/chat" + since
    );
  }

  async function sendChat(tradeId, body, file) {
    const url = "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/chat";
    let res;
    if (file) {
      const form = new FormData();
      if (body) form.append("body", body);
      form.append("file", file, file.name || "attachment");
      res = await fetch(url, { method: "POST", body: form });
    } else {
      res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: body || "" }),
      });
    }
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || !data.success) {
      const err = new Error(data.error || ("HTTP " + res.status));
      err.data = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function _signTradeAction(tradeId, prepUrl) {
    const prep = await jsonPost(prepUrl);
    
    // Handle rate limiting response
    if (prep.rate_limited) {
      const err = new Error(prep.error || "Rate limited");
      err.rate_limited = true;
      err.seconds_remaining = prep.seconds_remaining || 3;
      throw err;
    }
    
    if (!prep.success) {
      throw new Error(prep.error || "Transaction preparation failed");
    }
    
    const txKey = Object.keys(prep.transactions || {})[0];
    const tx = prep.transactions[txKey];
    const txHash = await sendPreparedTx(tx);
    return { txHash, prep };
  }

  function markPaid(tradeId) {
    return _signTradeAction(
      tradeId,
      "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/prepare-mark-paid"
    );
  }
  function release(tradeId) {
    return _signTradeAction(
      tradeId,
      "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/prepare-release"
    );
  }
  function cancelOrder(tradeId) {
    return _signTradeAction(
      tradeId,
      "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/prepare-cancel"
    );
  }
  function dispute(tradeId) {
    return _signTradeAction(
      tradeId,
      "/p2p/api/trades/" + encodeURIComponent(tradeId) + "/prepare-dispute"
    );
  }
  function closeAd(orderId) {
    return _signTradeAction(
      orderId,
      "/p2p/api/ads/" + encodeURIComponent(orderId) + "/prepare-close"
    );
  }

  // ----- Read helpers -----

  function listAds(params) {
    const qs = new URLSearchParams(params || {}).toString();
    return jsonGet("/p2p/api/ads" + (qs ? "?" + qs : ""));
  }
  function myAds() { return jsonGet("/p2p/api/ads/mine"); }
  function myTrades() { return jsonGet("/p2p/api/trades/mine"); }
  function getOrder(orderId) {
    return jsonGet("/p2p/api/orders/" + encodeURIComponent(orderId));
  }
  function getTrade(tradeId) {
    return jsonGet("/p2p/api/trades/" + encodeURIComponent(tradeId));
  }
  function getContractInfo() { return jsonGet("/p2p/api/contract"); }

  window.P2PEscrow = {
    configure,
    sendPreparedTx,
    waitForReceipt,
    openAd,
    closeAd,
    placeOrder,
    uploadProof,
    uploadProofFile,
    listProofs,
    listChat,
    sendChat,
    markPaid,
    release,
    cancelOrder,
    dispute,
    listAds,
    myAds,
    myTrades,
    getOrder,
    getTrade,
    getContractInfo,
  };
})();
