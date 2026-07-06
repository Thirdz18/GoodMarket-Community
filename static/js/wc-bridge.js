/**
 * GoodMarket WalletConnect Bridge
 * ---------------------------------------------------------------------------
 * Shared EIP-1193-shaped provider for users that logged in via WalletConnect
 * (or "manual address" mode) and therefore do NOT have `window.ethereum`
 * injected. Without this bridge, action buttons on Claim G$, Send, Swap,
 * Savings, etc. all fail with "No wallet detected" — even though the user
 * has already approved a WalletConnect session at login.
 *
 * The bridge talks to the same Node sidecar service as the homepage login
 * flow (/api/wc-uri, /api/wc-session/<id>, /api/wc-tx/<id>) when available,
 * and falls back to the in-browser WalletConnect v2 SignClient SDK in
 * serverless deployments where the sidecar is not running.
 *
 * Usage from a template:
 *
 *   <script src="{{ url_for('static', filename='js/wc-bridge.js', v=ASSET_VERSION) }}"></script>
 *   <script>
 *     GMWalletConnect.configure({
 *       walletAddress: "{{ wallet }}",
 *       loginMethod: "{{ login_method }}",
 *       projectId: "{{ walletconnect_project_id }}",
 *       dappName: "GoodMarket — Claim",
 *       dappDescription: "Claim daily G$ on Celo",
 *       assetVersion: "{{ ASSET_VERSION }}",
 *     });
 *   </script>
 *
 * Then any code path that previously did:
 *
 *   const provider = await _vAwaitEthProvider();
 *   if (!provider) throw new Error('No wallet detected');
 *
 * can fall back to:
 *
 *   const provider =
 *     (await _vAwaitEthProvider()) ||
 *     (GMWalletConnect.isPreferred() ? await GMWalletConnect.getProvider() : null);
 *
 * The returned object exposes a `request({ method, params })` function
 * compatible with EIP-1193 / `window.ethereum`, so existing tx/sign code
 * keeps working unchanged.
 */
(function (global) {
    "use strict";

    if (global.GMWalletConnect) return;

    var DEFAULT_RPC_URL = "https://forno.celo.org";
    var DEFAULT_CHAIN_HEX = "0xa4ec";
    var DEFAULT_CHAIN_ID = 42220;
    var WC_CDN_URL = "https://cdn.jsdelivr.net/npm/@walletconnect/sign-client@2.17.0/dist/index.umd.js";

    // Celo RPC fallback URLs for reliability
    var CELO_RPC_URLS = [
        "https://forno.celo.org",
        "https://1rpc.io/celo",
        "https://celo.publicnode.com"
    ];

    // Supported networks for WalletConnect chain switching
    var SUPPORTED_NETWORKS = {
        "0xa4ec": { // Celo Mainnet
            name: "Celo",
            chainId: 42220,
            rpc: "https://forno.celo.org",
            rpcUrls: CELO_RPC_URLS,
            nativeCurrency: {
                name: "Celo",
                symbol: "CELO",
                decimals: 18
            },
            blockExplorerUrls: ["https://celoscan.io"]
        },
        "0x32": { // XDC Network Mainnet
            name: "XDC Network",
            chainId: 50,
            rpc: "https://earpc.xinfin.network",
            nativeCurrency: {
                name: "XDC",
                symbol: "XDC",
                decimals: 18
            },
            blockExplorerUrls: ["https://xdcscan.io"]
        },
        "0x2105": { // Base Mainnet
            name: "Base",
            chainId: 8453,
            rpc: "https://mainnet.base.org",
            nativeCurrency: {
                name: "Ether",
                symbol: "ETH",
                decimals: 18
            },
            blockExplorerUrls: ["https://basescan.org"]
        }
    };

    var _config = {
        walletAddress: "",
        loginMethod: "",
        projectId: "",
        dappName: "GoodMarket",
        dappDescription: "GoodMarket on Celo",
        dappUrl: "",
        dappIcon: "",
        assetVersion: "",
        sidecarEnabled: true,
        // Caller can override how WalletConnect QR codes are surfaced. The
        // default writes the URI into a small floating modal; templates that
        // already manage their own status banners can pass a callback that
        // renders the QR inline instead.
        showQr: null,
        hideQr: null,
        log: function () {},
        chainHex: DEFAULT_CHAIN_HEX,
        chainId: DEFAULT_CHAIN_ID,
        rpcUrl: DEFAULT_RPC_URL,
    };

    var _state = {
        sessionId: null,
        address: null,
        mode: null,             // "sidecar" | "browser"
        signClient: null,
        browserSession: null,
        sdkLoading: null,
    };

    function _wcExtendIfExpiring(client, topic, session) {
        if (!client || !topic || !session || !session.expiry) return;
        var secsLeft = session.expiry - Math.floor(Date.now() / 1000);
        if (secsLeft <= 0 || secsLeft > 2 * 24 * 3600) return;
        try { _config.log('[wc-bridge] Session expires in', Math.round(secsLeft / 3600), 'h — requesting extension...'); } catch (_) {}
        client.extend({ topic: topic }).then(function() {
            try { _config.log('[wc-bridge] Session extended successfully'); } catch (_) {}
            try { localStorage.setItem('wc_session_timestamp', Date.now().toString()); } catch (_) {}
            try {
                var active = client.getActiveSessions ? client.getActiveSessions() : {};
                var updated = active[topic];
                if (!updated && client.session && client.session.getAll) {
                    updated = client.session.getAll().find(function(x) { return x.topic === topic; });
                }
                if (updated) localStorage.setItem('wc_session_data', JSON.stringify(updated));
            } catch (_) {}
        }).catch(function(e) { try { _config.log('[wc-bridge] Extend failed:', e && e.message); } catch (_) {} });
    }

    function _normLogin(method) {
        return String(method == null ? "" : method).toLowerCase();
    }

    function _shouldPrefer() {
        return ["walletconnect", "manual", "manual_address"].indexOf(_normLogin(_config.loginMethod)) >= 0;
    }

    // Is there a valid, persisted WalletConnect session for `walletAddr`
    // (defaults to the configured GoodMarket wallet)? The homepage writes
    // wc_session_* to localStorage on every WalletConnect login.
    function _hasStoredWcSessionFor(walletAddr) {
        try {
            var topic = localStorage.getItem("wc_session_topic");
            var data = localStorage.getItem("wc_session_data");
            if (!topic || !data) return false;
            var ts = parseInt(localStorage.getItem("wc_session_timestamp") || "0", 10);
            if (ts && (Date.now() - ts) > 7 * 24 * 60 * 60 * 1000) return false;
            try {
                var parsed = JSON.parse(data);
                var exp = parsed && parsed.expiry;
                if (exp && exp <= Math.floor(Date.now() / 1000)) return false;
            } catch (_) { /* tolerate a malformed session blob */ }
            var want = String(walletAddr || _config.walletAddress || "").toLowerCase();
            var have = String(localStorage.getItem("wc_session_address") || "").toLowerCase();
            if (want && have && want !== have) return false;
            return true;
        } catch (_) {
            return false;
        }
    }

    // Robust "should this user sign via WalletConnect?" check.
    //
    // `login_method` is the primary signal, but sessions created BEFORE it was
    // persisted server-side report "injected" for everyone (the old backend
    // default). Those WalletConnect users would otherwise sign with a desktop
    // MetaMask extension on a different account → "Wrong wallet connected". So
    // we also treat a valid, persisted WalletConnect session for the logged-in
    // wallet as proof of a WalletConnect login. A genuine injected-only user has no
    // such WC session, so their flow is unchanged.
    function _prefersWcSigning() {
        var m = _normLogin(_config.loginMethod);
        if (["walletconnect", "manual", "manual_address"].indexOf(m) >= 0) return true;
        return _hasStoredWcSessionFor(_config.walletAddress);
    }

    // Remove any persisted WalletConnect session. The homepage calls this right
    // after a successful INJECTED login so a leftover WC session from an earlier
    // sign-in can't later mis-route an injected user through WalletConnect.
    function _clearStoredSession() {
        try {
            ["wc_session_topic", "wc_session_address", "wc_session_data",
             "wc_session_timestamp", "wc_session_chains"].forEach(function (k) {
                localStorage.removeItem(k);
            });
        } catch (_) {}
        reset();
    }

    function configure(opts) {
        if (!opts) return;
        for (var k in opts) {
            if (Object.prototype.hasOwnProperty.call(opts, k)) {
                _config[k] = opts[k];
            }
        }
    }

    function _delay(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    // Wrap a promise with a timeout to prevent hanging requests
    function _withTimeout(promise, timeoutMs, timeoutMessage) {
        return Promise.race([
            promise,
            _delay(timeoutMs).then(function () {
                throw new Error(timeoutMessage || "Request timeout");
            })
        ]);
    }

    // ── Mobile-wallet-wake helpers ─────────────────────────────────────────
    // After firing a wallet-scoped WalletConnect request (eth_sendTransaction
    // / personal_sign / etc.) the wallet receives the message via the relay
    // *but* it does not always auto-foreground on mobile. The user is left
    // staring at the dApp browser with no sign request in sight.
    //
    // The remedy that the official @walletconnect/web3modal does is: after
    // initiating each `client.request(...)`, deep-link to the wallet's
    // `session.peer.metadata.redirect.native` (custom URL scheme) — or
    // `redirect.universal` (https universal link) as a fallback — to bring
    // the wallet to the foreground so the user actually sees the prompt.
    // This is what makes WalletConnect feel "click → approve" on Trust
    // Wallet / MetaMask / Valora etc. instead of "click → nothing happens".
    function _isMobileBrowserContext() {
        try {
            var ua = (typeof navigator !== "undefined" && navigator.userAgent) || "";
            if (!ua) return false;
            return /android|iphone|ipad|ipod|mobile/i.test(ua);
        } catch (_) { return false; }
    }

    function _wakeWalletAppFor(session) {
        try {
            if (!session || !_isMobileBrowserContext()) return;
            var meta = session.peer && session.peer.metadata;
            var redirect = meta && meta.redirect;
            if (!redirect) return;
            var href = redirect.native || redirect.universal;
            if (!href) return;
            // Use a synthetic `<a>` click so the OS treats this as a user
            // gesture and opens the wallet app via the custom scheme
            // (metamask://, trust://, etc.) without trying to navigate the
            // dApp tab away. This is the same trick @walletconnect/modal
            // uses internally; `window.location.href = …` is less reliable
            // because some browsers block custom-scheme navigation when no
            // user-gesture is in scope by the time the deep-link fires.
            try {
                var link = document.createElement("a");
                link.href = href;
                link.style.display = "none";
                link.target = "_self";
                link.rel = "noopener noreferrer";
                document.body.appendChild(link);
                link.click();
                setTimeout(function () { try { link.remove(); } catch (_) {} }, 100);
            } catch (_) { /* no-op */ }
        } catch (_) { /* no-op */ }
    }

    function _appendScript(src) {
        return new Promise(function (resolve, reject) {
            var s = document.createElement("script");
            s.src = src;
            s.onload = resolve;
            s.onerror = function () { reject(new Error("Failed to load " + src)); };
            document.head.appendChild(s);
        });
    }

    function _wcLoadSdk() {
        if (_state.sdkLoading) return _state.sdkLoading;
        _state.sdkLoading = (function () {
            var localSrc = null;
            try {
                localSrc = "/static/js/wc-bundle.js" + (_config.assetVersion ? ("?v=" + encodeURIComponent(_config.assetVersion)) : "");
            } catch (_) { /* no-op */ }

            function pick() {
                var ns = global["@walletconnect/sign-client"];
                return (ns && ns.SignClient) || null;
            }
            return Promise.resolve()
                .then(function () {
                    if (pick()) return pick();
                    if (localSrc) {
                        return _appendScript(localSrc).then(function () {
                            return pick();
                        }, function () { return null; });
                    }
                    return null;
                })
                .then(function (sc) {
                    if (sc) return sc;
                    return _appendScript(WC_CDN_URL).then(function () {
                        var sc2 = pick();
                        if (!sc2) throw new Error("WalletConnect SDK unavailable");
                        return sc2;
                    });
                });
        })();
        return _state.sdkLoading;
    }

    // ── Session restore helpers ────────────────────────────────────────────────

    // Extract the address from a WalletConnect session's namespaces object.
    function _addrFromSession(session) {
        try {
            var ns = session && (session.namespaces || {});
            var addr = null;
            Object.keys(ns).some(function (key) {
                var accts = (ns[key] && ns[key].accounts) || [];
                if (accts.length) {
                    addr = String(accts[0]).split(":").pop();
                    return true;
                }
                return false;
            });
            return addr;
        } catch (_) { return null; }
    }

    // Pick the best session from a sessions map, preferring the one whose
    // address matches _config.walletAddress (when set).
    function _pickBestSession(sessions) {
        var nowSec = Math.floor(Date.now() / 1000);
        var keys = Object.keys(sessions || {}).filter(function (k) {
            var s = sessions[k];
            return s && s.topic && (!s.expiry || s.expiry > nowSec);
        });
        if (!keys.length) return null;

        var wantedAddr = String(_config.walletAddress || "").toLowerCase();
        if (wantedAddr) {
            for (var i = 0; i < keys.length; i++) {
                var s = sessions[keys[i]];
                var ns = s && (s.namespaces || {});
                var matched = Object.keys(ns).some(function (k) {
                    return ((ns[k] && ns[k].accounts) || []).some(function (a) {
                        return String(a).split(":").pop().toLowerCase() === wantedAddr;
                    });
                });
                if (matched) return s;
            }
        }
        return sessions[keys[0]];
    }

    // Poll getActiveSessions() for up to `maxMs` ms, retrying every `intervalMs`.
    // WalletConnect v2 uses IndexedDB for session storage; on a freshly initialised
    // client the IndexedDB read may not have completed by the time init() resolves,
    // so calling getActiveSessions() immediately returns {}. This poller gives the
    // SDK the time it needs to load persisted sessions from IndexedDB and reconnect
    // to the relay WebSocket before we conclude "no session available".
    function _pollForSessions(client, maxMs, intervalMs) {
        var start = Date.now();
        var interval = intervalMs || 300;
        var max = maxMs || 3000;

        function attempt() {
            try {
                var sessions = client.getActiveSessions();
                var session = _pickBestSession(sessions);
                if (session) return Promise.resolve(session);
            } catch (_) { /* ignore */ }

            if (Date.now() - start >= max) return Promise.resolve(null);
            return _delay(interval).then(attempt);
        }
        return attempt();
    }

    function _wcGetClient() {
        if (_state.signClient) return Promise.resolve(_state.signClient);
        if (!_config.projectId) {
            return Promise.reject(new Error("WALLETCONNECT_PROJECT_ID is not configured"));
        }
        return _wcLoadSdk().then(function (SignClient) {
            return SignClient.init({
                projectId: _config.projectId,
                metadata: {
                    name: 'GoodMarket',
                    description: 'GoodMarket on Celo',
                    url: (typeof window !== "undefined" ? window.location.origin : ""),
                    icons: [(typeof window !== "undefined" ? window.location.origin : "") + "/static/icons/icon-192x192.png"]
                }
            });
        }).then(function (client) {
            _state.signClient = client;

            // ── Step 0: Check localStorage FIRST (fast path) ────────────────────
            // localStorage is written synchronously by homepage at login, so it's
            // available immediately. This avoids waiting for IndexedDB which may
            // not have persisted yet on low-end devices.
            try {
                var storedTopic = localStorage.getItem('wc_session_topic');
                var storedAddress = localStorage.getItem('wc_session_address');
                var storedSessionData = localStorage.getItem('wc_session_data');
                var storedTimestamp = parseInt(localStorage.getItem('wc_session_timestamp') || '0', 10);

                var MAX_SESSION_AGE_MS = 7 * 24 * 60 * 60 * 1000;
                var sessionAge = storedTimestamp ? (Date.now() - storedTimestamp) : Infinity;
                var sessionTooOld = sessionAge > MAX_SESSION_AGE_MS;

                if (storedTopic && storedAddress && !sessionTooOld && storedSessionData) {
                    try {
                        var parsedSession = JSON.parse(storedSessionData);
                        var parsedExpiry = parsedSession && parsedSession.expiry;
                        var nowSec = Math.floor(Date.now() / 1000);
                        if (parsedSession && parsedSession.topic && (!parsedExpiry || parsedExpiry > nowSec)) {
                            // Check if SDK already has this session loaded from IndexedDB.
                            // getActiveSessions() is the public API; session.getAll() is internal.
                            var sdkSessions = {};
                            try {
                                var activeSessions = client.getActiveSessions();
                                if (activeSessions && typeof activeSessions === 'object') {
                                    Object.keys(activeSessions).forEach(function(k) {
                                        sdkSessions[k] = activeSessions[k];
                                    });
                                }
                            } catch (_) { /* SDK API may not be ready yet */ }
                            // Fallback to internal API if needed
                            if (Object.keys(sdkSessions).length === 0) {
                                try {
                                    if (client.session && typeof client.session.getAll === 'function') {
                                        var allSessions = client.session.getAll();
                                        if (allSessions && allSessions.length) {
                                            allSessions.forEach(function(s) { if (s && s.topic) sdkSessions[s.topic] = s; });
                                        }
                                    }
                                } catch (_) { /* ignore */ }
                            }

                            // If SDK has the session, use it (best case - relay is connected)
                            if (sdkSessions[storedTopic]) {
                                _state.browserSession = sdkSessions[storedTopic];
                                _state.address = storedAddress;
                                _state.mode = "browser";
                                try { _config.log("[wc-bridge] Using SDK session (relay connected):", storedTopic); } catch (_) {}
                                _wcExtendIfExpiring(client, storedTopic, _state.browserSession);
                                return client;
                            }

                            // If SDK doesn't have the session yet, try to restore from localStorage.
                            // We need to: 1) Set the session in the internal store, 2) Subscribe to relay.
                            if (parsedSession.topic === storedTopic) {
                                // First, subscribe to the relay topic - this establishes the WebSocket
                                // connection needed for requests. This is critical and must happen first.
                                var subscribed = false;
                                try {
                                    if (client.core && client.core.relayer) {
                                        var relayer = client.core.relayer;
                                        // Check if there's a transport we can use
                                        if (typeof relayer.subscribe === 'function') {
                                            // Close any existing subscription for this topic first
                                            try { relayer.unsubscribe && relayer.unsubscribe(storedTopic); } catch (_) { /* ignore */ }
                                            // Subscribe to the topic - this re-establishes the relay connection
                                            relayer.subscribe(storedTopic).then(function() {
                                                subscribed = true;
                                                try { _config.log("[wc-bridge] Relay subscription established:", storedTopic); } catch (_) {}
                                            }).catch(function(err) {
                                                try { _config.log("[wc-bridge] Relay subscribe failed:", err && err.message); } catch (_) {}
                                            });
                                        }
                                        // Also try legacy API if available
                                        else if (typeof relayer.transportSubscribe === 'function') {
                                            relayer.transportSubscribe(storedTopic).then(function() {
                                                subscribed = true;
                                            }).catch(function(_){});
                                        }
                                    }
                                } catch (_) { /* ignore */ }

                                // Second, inject the session into the client's session store
                                try {
                                    if (client.session) {
                                        // Try various internal APIs that might work
                                        if (typeof client.session.set === 'function') {
                                            client.session.set(parsedSession.topic, parsedSession);
                                        }
                                        // Some SDK versions use a different method
                                        if (typeof client.session.setSession === 'function') {
                                            client.session.setSession(parsedSession);
                                        }
                                        // Another possible API
                                        if (typeof client.session.persist === 'function') {
                                            client.session.persist(parsedSession.topic, parsedSession);
                                        }
                                    }
                                } catch (_) { /* internal API may not exist in this version */ }

                                // Mark as restored - the actual relay connection is async
                                _state.browserSession = parsedSession;
                                _state.address = storedAddress;
                                _state.mode = "browser";
                                try { _config.log("[wc-bridge] Restored WC session from localStorage:", storedTopic); } catch (_) {}
                                _wcExtendIfExpiring(client, storedTopic, _state.browserSession);
                                return client;
                            }
                        }
                    } catch (parseErr) { 
                        try { _config.log("[wc-bridge] localStorage parse error:", parseErr && parseErr.message); } catch (_) {} 
                    }
                }

                // Clean up stale localStorage
                if (storedTopic && (sessionTooOld || !storedSessionData)) {
                    try {
                        localStorage.removeItem('wc_session_topic');
                        localStorage.removeItem('wc_session_address');
                        localStorage.removeItem('wc_session_data');
                        localStorage.removeItem('wc_session_timestamp');
                        localStorage.removeItem('wc_session_chains');
                    } catch (_) {}
                }
            } catch (lsErr) { 
                try { _config.log("[wc-bridge] localStorage access error:", lsErr && lsErr.message); } catch (_) {} 
            }

            // ── Step 1: Poll IndexedDB-backed sessions (SDK storage) ─────────
            // Poll for up to 3 s - the SDK reads from IndexedDB asynchronously.
            return _pollForSessions(client, 3000, 300).then(function (liveSession) {
                if (liveSession) {
                    _state.browserSession = liveSession;
                    _state.address = _addrFromSession(liveSession);
                    _state.mode = "browser";
                    try { _config.log("[wc-bridge] Restored live WC session from SDK storage:", liveSession.topic); } catch (_) {}
                    return client;
                }

                // No session found anywhere - this is expected for new users
                try { _config.log("[wc-bridge] No WalletConnect session found in SDK or localStorage"); } catch (_) {}
                return client;
            });
        });
    }

    // RPC call with fallback - tries each URL until one succeeds
    function _celoJsonRpcWithFallback(urls, method, params) {
        var lastError = new Error('All RPC endpoints failed');
        return new Promise(function(resolve, reject) {
            function tryNext(index) {
                if (index >= urls.length) {
                    reject(lastError);
                    return;
                }
                var url = urls[index];
                fetch(url, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        jsonrpc: "2.0",
                        id: Date.now(),
                        method: method,
                        params: params || []
                    })
                }).then(function (resp) {
                    return resp.json();
                }).then(function (data) {
                    if (data.error) {
                        lastError = new Error(data.error.message || "RPC error");
                        tryNext(index + 1); // Try next URL
                    } else {
                        resolve(data.result);
                    }
                }).catch(function(e) {
                    lastError = e;
                    tryNext(index + 1); // Try next URL
                });
            }
            tryNext(0);
        });
    }

    function _celoJsonRpc(method, params) {
        // Use fallback RPCs - try config URL first, then fallbacks
        var urls = (_config.rpcUrl ? [_config.rpcUrl] : []).concat(CELO_RPC_URLS);
        return _celoJsonRpcWithFallback(urls, method, params);
    }

    function _defaultShowQr(uri, label) {
        try {
            if (_config.showQr) return _config.showQr(uri, label);
        } catch (_) { /* fall through to default */ }

        var existing = document.getElementById("__gmWcModal");
        if (existing) existing.remove();

        var showInstructions = false; // Track which view is displayed
        
        var modal = document.createElement("div");
        modal.id = "__gmWcModal";
        modal.setAttribute("role", "dialog");
        modal.style.cssText =
            "position:fixed;inset:0;z-index:2147483646;display:flex;align-items:center;" +
            "justify-content:center;background:rgba(2,6,23,0.85);padding:1rem;font-family:inherit;";

        var card = document.createElement("div");
        card.style.cssText =
            "background:#0f1a2e;border:1px solid rgba(124,58,237,0.35);border-radius:24px;" +
            "padding:2rem 2rem 1.8rem;max-width:380px;width:100%;text-align:center;color:#f8fafc;" +
            "box-shadow:0 24px 60px rgba(15,23,42,0.7),0 0 40px rgba(124,58,237,0.15);";

        var header = document.createElement("div");
        header.style.cssText = "display:flex;align-items:center;justify-content:space-between;margin-bottom:1.2rem;";

        var titleContainer = document.createElement("div");
        var title = document.createElement("div");
        title.textContent = "WalletConnect";
        title.style.cssText = "font-weight:700;font-size:1.1rem;color:#fff;";

        var backBtn = document.createElement("button");
        backBtn.innerHTML = "←";
        backBtn.type = "button";
        backBtn.style.cssText =
            "background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.12);" +
            "color:#f8fafc;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:1rem;" +
            "display:flex;align-items:center;justify-content:center;transition:all 0.2s;";
        backBtn.addEventListener("mouseover", function() { this.style.background = "rgba(255,255,255,0.1)"; });
        backBtn.addEventListener("mouseout", function() { this.style.background = "rgba(255,255,255,0.05)"; });
        backBtn.addEventListener("click", function() {
            showInstructions = !showInstructions;
            if (showInstructions) {
                // Show instructions view
                qrButtonContainer.style.display = "none";
                subtitle.textContent = "Better experience on native wallets";
                instructionsContainer.style.display = "block";
            } else {
                // Show QR view
                qrButtonContainer.style.display = "flex";
                subtitle.textContent = "Scan this code with your phone";
                instructionsContainer.style.display = "none";
            }
        });

        var closeBtn = document.createElement("button");
        closeBtn.innerHTML = "✕";
        closeBtn.type = "button";
        closeBtn.style.cssText =
            "background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.12);" +
            "color:#f8fafc;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:1.2rem;" +
            "display:flex;align-items:center;justify-content:center;transition:all 0.2s;";
        closeBtn.addEventListener("mouseover", function() { this.style.background = "rgba(255,255,255,0.1)"; });
        closeBtn.addEventListener("mouseout", function() { this.style.background = "rgba(255,255,255,0.05)"; });
        closeBtn.addEventListener("click", _defaultHideQr);

        header.appendChild(backBtn);
        header.appendChild(title);
        header.appendChild(closeBtn);

        var subtitle = document.createElement("div");
        subtitle.textContent = "Scan this code with your phone";
        subtitle.style.cssText = "font-size:0.75rem;line-height:1.4;color:rgba(248,250,252,0.6);margin-bottom:1.2rem;";

        // QR and Button Container
        var qrButtonContainer = document.createElement("div");
        qrButtonContainer.style.cssText =
            "display:flex;flex-direction:column;align-items:center;gap:0.8rem;margin-bottom:0.6rem;";

        // QR Container with WalletConnect logo overlay
        var qrContainer = document.createElement("div");
        qrContainer.style.cssText =
            "position:relative;width:240px;height:240px;" +
            "background:#fff;border-radius:16px;padding:12px;box-shadow:0 8px 32px rgba(0,0,0,0.3);";

        var img = document.createElement("img");
        img.alt = "WalletConnect QR";
        img.width = 216;
        img.height = 216;
        img.src = "https://api.qrserver.com/v1/create-qr-code/?size=216x216&data=" + encodeURIComponent(uri);
        img.style.cssText = "display:block;width:100%;height:100%;border-radius:8px;";

        // WalletConnect logo overlay (centered)
        var logoOverlay = document.createElement("div");
        logoOverlay.style.cssText =
            "position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);" +
            "width:56px;height:56px;background:#0f1a2e;border-radius:12px;" +
            "display:flex;align-items:center;justify-content:center;" +
            "font-size:2rem;box-shadow:0 4px 12px rgba(0,0,0,0.3);";
        logoOverlay.innerHTML = "🔗";

        qrContainer.appendChild(img);
        qrContainer.appendChild(logoOverlay);

        var copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.innerHTML = "📋 Copy link";
        copyBtn.style.cssText =
            "display:flex;align-items:center;justify-content:center;gap:0.5rem;" +
            "padding:0.65rem 1.2rem;border-radius:10px;border:1.5px solid rgba(124,58,237,0.5);" +
            "background:linear-gradient(135deg,rgba(124,58,237,0.2),rgba(99,102,241,0.1));" +
            "color:#e0e7ff;font-weight:600;font-size:0.85rem;cursor:pointer;" +
            "transition:all 0.2s;white-space:nowrap;backdrop-filter:blur(8px);";
        copyBtn.addEventListener("mouseover", function() {
            this.style.background = "linear-gradient(135deg,rgba(124,58,237,0.3),rgba(99,102,241,0.15))";
            this.style.borderColor = "rgba(124,58,237,0.7)";
            this.style.transform = "translateY(-2px)";
            this.style.boxShadow = "0 4px 12px rgba(124,58,237,0.3)";
        });
        copyBtn.addEventListener("mouseout", function() {
            this.style.background = "linear-gradient(135deg,rgba(124,58,237,0.2),rgba(99,102,241,0.1))";
            this.style.borderColor = "rgba(124,58,237,0.5)";
            this.style.transform = "translateY(0)";
            this.style.boxShadow = "none";
        });
        copyBtn.addEventListener("click", function () {
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(uri);
                } else {
                    var ta = document.createElement("textarea");
                    ta.value = uri;
                    ta.style.position = "fixed";
                    ta.style.left = "-9999px";
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand("copy");
                    document.body.removeChild(ta);
                }
                var origText = copyBtn.innerHTML;
                copyBtn.innerHTML = "✓ Copied!";
                copyBtn.style.background = "linear-gradient(135deg,rgba(16,185,129,0.25),rgba(16,185,129,0.1))";
                copyBtn.style.borderColor = "rgba(16,185,129,0.6)";
                copyBtn.style.color = "#86efac";
                copyBtn.style.transform = "translateY(0)";
                setTimeout(function () {
                    copyBtn.innerHTML = origText;
                    copyBtn.style.background = "linear-gradient(135deg,rgba(124,58,237,0.2),rgba(99,102,241,0.1))";
                    copyBtn.style.borderColor = "rgba(124,58,237,0.5)";
                    copyBtn.style.color = "#e0e7ff";
                }, 1800);
            } catch (_) { /* no-op */ }
        });

        qrButtonContainer.appendChild(qrContainer);
        qrButtonContainer.appendChild(copyBtn);

        // Instructions Container
        var instructionsContainer = document.createElement("div");
        instructionsContainer.style.cssText =
            "display:none;text-align:left;font-size:0.7rem;line-height:1.6;color:rgba(248,250,252,0.8);";

        var trustWalletSection = document.createElement("div");
        trustWalletSection.style.cssText = "margin-bottom:1rem;padding-bottom:1rem;border-bottom:1px solid rgba(124,58,237,0.2);";
        trustWalletSection.innerHTML =
            "<strong style='color:#e0e7ff;font-size:0.75rem;'>1. Trust Wallet</strong><br>" +
            "• Open Trust Wallet app<br>" +
            "• Tap \"Discover\" button (bottom right)<br>" +
            "• Search for or tap \"goodmarket.live\"<br>" +
            "• Better transaction experience";

        var metamaskSection = document.createElement("div");
        metamaskSection.style.cssText = "margin-bottom:0.5rem;";
        metamaskSection.innerHTML =
            "<strong style='color:#e0e7ff;font-size:0.75rem;'>2. MetaMask Mobile</strong><br>" +
            "• Open MetaMask wallet app<br>" +
            "• Tap \"Explore\" icon<br>" +
            "• Paste https://goodmarket.live<br>" +
            "• Better transaction experience";

        instructionsContainer.appendChild(trustWalletSection);
        instructionsContainer.appendChild(metamaskSection);

        card.appendChild(header);
        card.appendChild(subtitle);
        card.appendChild(qrButtonContainer);
        card.appendChild(instructionsContainer);
        modal.appendChild(card);
        document.body.appendChild(modal);
    }

    function _defaultHideQr() {
        try {
            if (_config.hideQr) return _config.hideQr();
        } catch (_) { /* fall through to default */ }
        var existing = document.getElementById("__gmWcModal");
        if (existing) existing.remove();
    }

    function reset() {
        _state.sessionId = null;
        _state.address = null;
        _state.mode = null;
        _state.browserSession = null;
    }

    function connect() {
        // Only short-circuit if there is an active session backing the address.
        var hasActiveSession =
            (_state.mode === "sidecar" && !!_state.sessionId) ||
            (_state.mode === "browser" && !!_state.browserSession);
        if (hasActiveSession && _state.address) return Promise.resolve(_state.address);
        
        var wantedAddress = String(_config.walletAddress || "").toLowerCase();
        
        // First, try to restore existing session from SignClient.
        // This is critical to prevent QR modal from appearing during signing.
        return _wcGetClient().then(function (client) {
            // Check if session was restored in _wcGetClient
            if (_state.browserSession && _state.address) {
                _state.mode = "browser";
                return _state.address;
            }
            
            // If sidecar is disabled (for signing pages like wallet.html),
            // don't try to create new session - just return error
            if (_config.sidecarEnabled === false) {
                throw new Error("No active WalletConnect session. Please refresh and try again.");
            }
            
            // Continue with sidecar (for login pages that need QR)
            return null;
        }).then(function (restoredAddr) {
            if (restoredAddr) return restoredAddr;
            
            // Try Node sidecar first (for login pages)
            var sidecarPromise = Promise.resolve(null);
            if (_config.sidecarEnabled !== false) {
                sidecarPromise = (function () {
                    return fetch("/api/wc-uri")
                        .then(function (resp) {
                            if (!resp.ok) throw new Error("WalletConnect sidecar unavailable");
                            return resp.json();
                        })
                        .then(function (data) {
                            if (!data || !data.success || !data.id || !data.uri) {
                                throw new Error((data && data.error) || "WalletConnect sidecar unavailable");
                            }
                            _state.sessionId = data.id;
                            _state.mode = "sidecar";
                            _defaultShowQr(data.uri, "Approve in your wallet");

                            var pollStart = Date.now();
                            var deadline = pollStart + 120000;
                            function poll() {
                                var dt = Date.now() - pollStart;
                                if (Date.now() >= deadline) {
                                    throw new Error("WalletConnect approval timed out.");
                                }
                                var pollAttempts = Math.floor(dt / 200);
                                var delayMs = Math.min(200 + (pollAttempts * 50), 2000);
                                return _delay(delayMs)
                                    .then(function () { return fetch("/api/wc-session/" + encodeURIComponent(_state.sessionId)); })
                                    .then(function (r) { return r.json(); })
                                    .then(function (st) {
                                        if (!st || !st.success) return poll();
                                        if (st.status === "approved" && st.address) {
                                            _state.address = st.address;
                                            return _state.address;
                                        }
                                        if (st.status === "rejected") {
                                            throw new Error("WalletConnect request was rejected.");
                                        }
                                        return poll();
                                    });
                            }
                            return poll();
                        })
                        .then(function (addr) {
                            _defaultHideQr();
                            return addr;
                        })
                        .catch(function (err) {
                            _defaultHideQr();
                            try { _config.log("wc-bridge sidecar fallback:", err && err.message); } catch (_) {}
                            return null;
                        });
                })();
            }

            return sidecarPromise;
        }).then(function (addr) {
            if (addr) return addr;
            
            // For browser SDK path (should rarely be reached now with session restoration)
            if (_state.browserSession && _state.address) {
                _state.mode = "browser";
                return _state.address;
            }
            
            // If we get here and no session, throw error (no QR)
            throw new Error("No active WalletConnect session. Please refresh and try again.");
        }).then(function (addr) {
            if (wantedAddress && addr && String(addr).toLowerCase() !== wantedAddress) {
                reset();
                throw new Error("Wrong WalletConnect wallet connected. Please connect your GoodMarket wallet.");
            }
            return addr;
        });
    }

    function _walletScopedMethod(method) {
        // Methods that must be answered by the wallet (not Celo RPC).
        return method === "eth_sendTransaction" ||
               method === "personal_sign" ||
               method === "eth_sign" ||
               method === "eth_signTypedData" ||
               method === "eth_signTypedData_v3" ||
               method === "eth_signTypedData_v4";
    }

    // EIP-1193-shaped error so ethers.js BrowserProvider can recognise it
    // (e.g. 4001 → user rejected) and avoid the opaque
    // "could not coalesce error" wrapping that confuses end users.
    function _wcRpcError(message, sourceErr, fallbackCode) {
        var src = sourceErr;
        if (typeof src === "string") src = { message: src };
        var code;
        if (src && typeof src.code === "number") code = src.code;
        else code = (typeof fallbackCode === "number") ? fallbackCode : -32603;
        var msg = message || (src && src.message) || "WalletConnect request failed";
        // Heuristic: any rejection-y phrase → 4001 (user rejected) so the
        // friendly cancellation copy fires across all surfaces.
        if (/user rejected|user denied|user disapproved|rejected by user|user closed|user cancel|cancelled|canceled/i.test(String(msg))) {
            code = 4001;
        }
        var e = new Error(String(msg));
        e.code = code;
        if (src && src.data !== undefined) e.data = src.data;
        return e;
    }

    function bridgeRequest(method, params) {
        var p = params || [];

        if (method === "eth_accounts" || method === "eth_requestAccounts") {
            // Return the known wallet address immediately without triggering a new
            // WalletConnect session (QR scan). The WC session is only established
            // on demand when a real wallet-scoped action (eth_sendTransaction /
            // personal_sign) is first needed. This prevents an unexpected QR modal
            // appearing for users who already logged in via WalletConnect.
            if (_config.walletAddress) {
                return Promise.resolve([_config.walletAddress]);
            }
            return connect().then(function (addr) { return [addr]; });
        }
        if (method === "eth_chainId") {
            return Promise.resolve(String(_config.chainHex || DEFAULT_CHAIN_HEX));
        }
        if (method === "net_version") {
            return Promise.resolve(String(Number(_config.chainId || DEFAULT_CHAIN_ID)));
        }
        if (method === "wallet_switchEthereumChain") {
            // Handle network switching for supported chains
            var chainId = (p && p[0] && p[0].chainId) ? String(p[0].chainId) : null;
            if (!chainId) {
                return Promise.reject(_wcRpcError("Missing chainId parameter", null, -32602));
            }
            // Validate that the network is supported
            if (SUPPORTED_NETWORKS[chainId]) {
                // Update _config to reflect the new chain so subsequent requests
                // (eth_chainId, eth_sendTransaction, etc.) use the correct chain
                var newChainId = SUPPORTED_NETWORKS[chainId].chainId;
                var newChainHex = chainId; // Already in hex format like "0x32"
                _config.chainId = newChainId;
                _config.chainHex = newChainHex;
                try { _config.log("[wc-bridge] Switched to chainId:", newChainId, "hex:", newChainHex); } catch (_) {}
                // Return success - let the wallet handle the actual switch
                return Promise.resolve(null);
            }
            // Return error code 4902 (unrecognized chain) per EIP-3326
            return Promise.reject(_wcRpcError("Unrecognized chain ID. Add the chain with wallet_addEthereumChain first.", null, 4902));
        }
        if (method === "wallet_addEthereumChain") {
            // Handle adding a new network
            var chainData = (p && p[0]) ? p[0] : {};
            if (!chainData.chainId) {
                return Promise.reject(_wcRpcError("Missing chainId in chain parameters", null, -32602));
            }
            // Validate required fields per EIP-3085
            if (!chainData.chainName || !chainData.rpcUrls || chainData.rpcUrls.length === 0) {
                return Promise.reject(_wcRpcError("Missing required chain parameters (chainName or rpcUrls)", null, -32602));
            }
            if (!chainData.nativeCurrency) {
                return Promise.reject(_wcRpcError("Missing nativeCurrency in chain parameters", null, -32602));
            }
            // For now, accept the chain addition (wallet will handle the actual add)
            // In the future, could store in SUPPORTED_NETWORKS for future validation
            return Promise.resolve(null);
        }

        if (_walletScopedMethod(method)) {
            return connect().then(function () {
                if (_state.mode === "sidecar" && method === "eth_sendTransaction") {
                    var txParams = (p && p[0]) ? p[0] : {};
                    return fetch("/api/wc-tx/" + encodeURIComponent(_state.sessionId), {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(txParams)
                    }).then(function (r) { return r.json(); }).then(function (d) {
                        if (!d || !d.success || d.error || !d.txHash) {
                            throw _wcRpcError((d && d.error) || "WalletConnect transaction failed", d && d.error);
                        }
                        return d.txHash;
                    });
                }
                if (_state.mode === "sidecar" && method === "personal_sign") {
                    // The sidecar /sign endpoint expects { message, address }.
                    // personal_sign params can be (message, address) OR (address, message).
                    var a = p[0];
                    var b = p[1];
                    var maybeAddr = function (v) {
                        return typeof v === "string" && /^0x[0-9a-fA-F]{40}$/.test(v);
                    };
                    var address = maybeAddr(a) ? a : (maybeAddr(b) ? b : _state.address);
                    var message = (address === a) ? b : a;
                    if (typeof message === "string" && message.indexOf("0x") === 0) {
                        try {
                            var hex = message.slice(2);
                            var bytes = new Uint8Array(hex.length / 2);
                            for (var i = 0; i < bytes.length; i++) {
                                bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
                            }
                            message = new TextDecoder().decode(bytes);
                        } catch (_) { /* leave hex as-is if decode fails */ }
                    }
                    return fetch("/api/wc-sign/" + encodeURIComponent(_state.sessionId), {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ message: message, address: address })
                    }).then(function (r) { return r.json(); }).then(function (d) {
                        if (!d || !d.signature) {
                            throw _wcRpcError((d && d.error) || "WalletConnect signature failed", d && d.error);
                        }
                        var sig = d.signature;
                        return sig.indexOf("0x") === 0 ? sig : ("0x" + sig);
                    }).catch(function (err) {
                        // If sidecar fails (timeout or network), provide clearer error
                        throw _wcRpcError("Signature request failed: " + (err && err.message), err && err.message);
                    });
                }
                
                // Browser SDK path for wallet-scoped methods (eth_sendTransaction, personal_sign, etc.)
                // First, ensure we have a client initialized
                return _wcGetClient().then(function (client) {
                    // If no browser session yet, try to restore from SDK or localStorage
                    if (!_state.browserSession) {
                        // First, check if SDK has the session in its internal map
                        var sdkSession = null;
                        try {
                            if (client.session && typeof client.session.getAll === 'function') {
                                var sessions = client.session.getAll();
                                if (sessions && sessions.length) {
                                    // Find the session matching our stored address
                                    var wantedAddr = String(_config.walletAddress || "").toLowerCase();
                                    for (var i = 0; i < sessions.length; i++) {
                                        var s = sessions[i];
                                        var ns = s && (s.namespaces || {});
                                        var matched = Object.keys(ns).some(function (k) {
                                            return ((ns[k] && ns[k].accounts) || []).some(function (a) {
                                                return String(a).split(":").pop().toLowerCase() === wantedAddr;
                                            });
                                        });
                                        if (matched) {
                                            sdkSession = s;
                                            break;
                                        }
                                    }
                                    // Fallback to first session if no address match
                                    if (!sdkSession) sdkSession = sessions[sessions.length - 1];
                                }
                            }
                        } catch (_) { /* ignore */ }
                        
                        if (sdkSession) {
                            _state.browserSession = sdkSession;
                            _state.address = _addrFromSession(sdkSession);
                            _state.mode = "browser";
                            try { _config.log("[wc-bridge] Found session in SDK:", sdkSession.topic); } catch (_) {}
                        }
                    }
                    
                    // If we have a browser session, make the request
                    if (_state.browserSession) {
                        return _doWcRequest(client, method, p).catch(function (wcErr) {
                            var errMsg = (wcErr && wcErr.message) ? String(wcErr.message) : "";
                            var errCode = (wcErr && typeof wcErr.code === "number") ? wcErr.code : -32603;
                            
                            // Session expired or invalid - try one more time to restore
                            if (/unknown connector|session.*not found|no matching key|expired/i.test(errMsg)) {
                                _state.browserSession = null;
                                _state.mode = null;
                                
                                // Try restoring from localStorage one more time
                                try {
                                    var storedTopic = localStorage.getItem('wc_session_topic');
                                    var storedSessionData = localStorage.getItem('wc_session_data');
                                    if (storedTopic && storedSessionData) {
                                        var parsedSession = JSON.parse(storedSessionData);
                                        if (parsedSession && parsedSession.topic === storedTopic) {
                                            // Try to re-subscribe to the relay
                                            try {
                                                if (client.core && client.core.relayer && typeof client.core.relayer.subscribe === 'function') {
                                                    client.core.relayer.subscribe(storedTopic).catch(function(_){});
                                                }
                                            } catch (_) {}
                                            
                                            // Try to inject session
                                            try {
                                                if (client.session && typeof client.session.set === 'function') {
                                                    client.session.set(parsedSession.topic, parsedSession);
                                                }
                                            } catch (_) {}
                                            
                                            _state.browserSession = parsedSession;
                                            _state.address = localStorage.getItem('wc_session_address');
                                            _state.mode = "browser";
                                            
                                            // Wait a moment for relay to connect, then retry
                                            return _delay(500).then(function() {
                                                return _doWcRequest(client, method, p);
                                            }).catch(function(retryErr) {
                                                // Retry also failed - clear everything
                                                _state.browserSession = null;
                                                try {
                                                    localStorage.removeItem('wc_session_topic');
                                                    localStorage.removeItem('wc_session_address');
                                                    localStorage.removeItem('wc_session_data');
                                                    localStorage.removeItem('wc_session_timestamp');
                                                    localStorage.removeItem('wc_session_chains');
                                                } catch (_) {}
                                                throw _wcRpcError(
                                                    "Your WalletConnect session has expired. Please log out and log in again to reconnect your wallet.",
                                                    null, -32603
                                                );
                                            });
                                        }
                                    }
                                } catch (_) {}
                                
                                // Clean up stale localStorage
                                try {
                                    localStorage.removeItem('wc_session_topic');
                                    localStorage.removeItem('wc_session_address');
                                    localStorage.removeItem('wc_session_data');
                                    localStorage.removeItem('wc_session_timestamp');
                                    localStorage.removeItem('wc_session_chains');
                                } catch (_) {}
                                
                                throw _wcRpcError(
                                    "Your WalletConnect session has expired. Please log out and log in again to reconnect your wallet.",
                                    null, -32603
                                );
                            }
                            
                            throw _wcRpcError(errMsg || "WalletConnect request failed", errMsg, errCode);
                        });
                    }
                    
                    // No session available - fail gracefully without showing QR.
                    // Transaction signing should NEVER require user to scan new QR.
                    // If localStorage has a session but we couldn't restore it, 
                    // provide a helpful message instead of confusing QR code.
                    var hasLocalStorageSession = false;
                    try {
                        hasLocalStorageSession = !!(localStorage.getItem('wc_session_topic') && localStorage.getItem('wc_session_data'));
                    } catch (_) {}
                    
                    if (hasLocalStorageSession) {
                        throw _wcRpcError(
                            "Unable to restore WalletConnect session. Please refresh the page and try again, or log out and log in again.",
                            null, -32603
                        );
                    }
                    
                    throw _wcRpcError("No active WalletConnect session. Please log out and log in again to reconnect your wallet.", null, -32603);
                });
            });
        }

        // Helper to make WC request - extracted for reuse on reconnect
        function _doWcRequest(client, method, p) {
            // Determine the target chain for this specific request.
            //
            // AUTOMATIC DETECTION based on request type:
            //
            // 1. wallet_switchEthereumChain / wallet_addEthereumChain:
            //    These methods contain the target chain in params[0].chainId.
            //    We MUST use this chain to route the request correctly, regardless
            //    of _config.chainId. Otherwise, switching to XDC would route to Celo.
            //
            // 2. eth_sendTransaction:
            //    If the caller provides chainId in tx params, we use it.
            //    Otherwise, use _config.chainId.
            //
            // 3. Other methods (personal_sign, eth_chainId, etc.):
            //    Use _config.chainId (defaults to Celo).
            //
            // This approach mirrors injected wallet behavior where network
            // operations are routed based on the actual network being targeted.
            var targetChainId = Number(_config.chainId || DEFAULT_CHAIN_ID);
            
            // For wallet_switchEthereumChain and wallet_addEthereumChain,
            // extract chainId from params - this is the network being switched to
            if ((method === "wallet_switchEthereumChain" || method === "wallet_addEthereumChain") &&
                Array.isArray(p) && p.length > 0 && p[0] && p[0].chainId) {
                var switchChain = parseInt(String(p[0].chainId), 16);
                if (!isNaN(switchChain) && switchChain > 0) targetChainId = switchChain;
            }
            // For eth_sendTransaction: chainId is in the tx params object
            else if (method === "eth_sendTransaction" && Array.isArray(p) && p.length > 0 &&
                p[0] && typeof p[0] === "object" && p[0].chainId) {
                var txChain = parseInt(String(p[0].chainId), 16);
                if (!isNaN(txChain) && txChain > 0) targetChainId = txChain;
            }
            // For other methods: check if first param is an object with chainId
            else if (Array.isArray(p) && p.length > 0 && p[0] && typeof p[0] === "object" && p[0].chainId) {
                var signChain = parseInt(String(p[0].chainId), 16);
                if (!isNaN(signChain) && signChain > 0) targetChainId = signChain;
            }

            // Debug logging for chain selection
            try { _config.log("[wc-bridge] Request: method=" + method + " targetChainId=" + targetChainId); } catch (_) {}

            // Wrap the request with a timeout to prevent the wallet
            // (MetaMask Mobile in particular) from hanging
            // indefinitely. The wallet sometimes silently drops
            // requests so we surface a clear failure after 45s.
            var requestPromise = client.request({
                topic: _state.browserSession.topic,
                chainId: "eip155:" + targetChainId,
                request: { method: method, params: p }
            });
            // Fire-and-forget: bring the wallet app to the foreground
            // on mobile so the user actually sees the sign / tx
            // prompt that's now sitting in the wallet's queue.
            // Without this MetaMask Mobile / Trust Wallet etc. are
            // happy to receive the request silently and the user
            // is left staring at the dApp tab thinking nothing
            // happened. This is what @walletconnect/modal does
            // internally — we do it manually because we use the
            // raw SignClient SDK.
            _wakeWalletAppFor(_state.browserSession);
            return _withTimeout(requestPromise, 45000,
                "WalletConnect request timeout (45s). Wallet may not respond to requests - try refreshing or switching wallets.");
        }

        // Read-only RPC calls — answer directly off Celo RPC to avoid an
        // extra wallet round-trip for things like eth_estimateGas.
        return _celoJsonRpc(method, p);
    }

    var _provider = null;
    function getProvider() {
        if (!_provider) {
            _provider = {
                isWalletConnect: true,
                isGoodMarketWcBridge: true,
                request: function (args) {
                    if (!args || !args.method) {
                        return Promise.reject(new Error("Missing method"));
                    }
                    return bridgeRequest(args.method, args.params);
                },
                // Convenience for ethers.js BrowserProvider compatibility:
                // some templates pass `{ request: fn }` directly into ethers,
                // which only checks for `request`. The other helpers below
                // are no-ops so existing wallet event listeners don't error.
                on: function () {},
                removeListener: function () {},
                removeAllListeners: function () {}
            };
        }
        return _provider;
    }

    // ── WalletConnect Session Expiry Guard ────────────────────────────────────
    // Automatically injected on every page that includes wc-bridge.js and calls
    // GMWalletConnect.configure() with loginMethod: "walletconnect".
    // - Shows an orange warning banner when the session expires in < 24 hours.
    // - Shows a red banner + 5-second countdown auto-logout when expired.
    // - Clears WC localStorage keys before redirecting to /logout.
    // No banner HTML is needed in templates — the guard creates it dynamically.

    var _guardTimer = null;
    var _autoLogoutTimer = null;
    var _BANNER_ID = 'gmWcExpiryBanner';

    function _guardClearWcStorage() {
        try {
            ['wc_session_topic', 'wc_session_address', 'wc_session_data',
             'wc_session_timestamp', 'wc_session_chains'].forEach(function (k) {
                localStorage.removeItem(k);
            });
        } catch (_) {}
    }

    function _guardGetOrCreateBanner() {
        var existing = document.getElementById(_BANNER_ID);
        if (existing) return existing;

        // Inject styles once
        var styleId = _BANNER_ID + '_style';
        if (!document.getElementById(styleId)) {
            var s = document.createElement('style');
            s.id = styleId;
            s.textContent = [
                '#' + _BANNER_ID + '{display:none;position:sticky;top:68px;margin:0.4rem 1.25rem 0.8rem;',
                'padding:0.72rem 0.85rem;border-radius:12px;font-size:0.8rem;line-height:1.45;',
                'font-weight:600;z-index:50;gap:0.5rem;align-items:center;flex-wrap:wrap;}',
                '#' + _BANNER_ID + '.gm-wc-warn{display:flex;background:rgba(249,115,22,0.12);',
                'border:1px solid rgba(249,115,22,0.40);color:#fdba74;}',
                '#' + _BANNER_ID + '.gm-wc-expired{display:flex;background:rgba(239,68,68,0.12);',
                'border:1px solid rgba(239,68,68,0.40);color:#fca5a5;}',
                '#' + _BANNER_ID + ' .gm-wc-msg{flex:1;min-width:0;}',
                '#' + _BANNER_ID + ' .gm-wc-btn{flex-shrink:0;padding:0.3rem 0.75rem;border-radius:8px;',
                'border:1px solid currentColor;background:transparent;color:inherit;',
                'font-size:0.78rem;font-weight:700;cursor:pointer;white-space:nowrap;}',
                '#' + _BANNER_ID + ' .gm-wc-dismiss{flex-shrink:0;background:none;border:none;',
                'color:inherit;opacity:0.6;font-size:1rem;cursor:pointer;padding:0 0.2rem;line-height:1;}'
            ].join('');
            document.head.appendChild(s);
        }

        // Build banner element
        var div = document.createElement('div');
        div.id = _BANNER_ID;
        div.setAttribute('role', 'alert');
        div.innerHTML =
            '<span class="gm-wc-msg" id="' + _BANNER_ID + '_msg"></span>' +
            '<button class="gm-wc-btn" id="' + _BANNER_ID + '_btn"></button>' +
            '<button class="gm-wc-dismiss" id="' + _BANNER_ID + '_x" title="Dismiss">&#x2715;</button>';

        // Insert right after <body> opens, or before first child
        var body = document.body;
        if (body) {
            body.insertBefore(div, body.firstChild);
        }

        // Wire up buttons
        var btn = document.getElementById(_BANNER_ID + '_btn');
        var x   = document.getElementById(_BANNER_ID + '_x');
        if (btn) btn.addEventListener('click', function () {
            _guardClearWcStorage();
            window.location.href = '/logout';
        });
        if (x) x.addEventListener('click', function () {
            div.classList.remove('gm-wc-warn', 'gm-wc-expired');
            // Re-check in 5 min — will re-show if still in warning, or auto-logout if expired
            if (_guardTimer) clearTimeout(_guardTimer);
            _guardTimer = setTimeout(_guardCheck, 5 * 60 * 1000);
        });

        return div;
    }

    function _guardStartAutoLogout(banner, msgEl, btnEl, xEl) {
        if (xEl)  xEl.style.display = 'none';
        if (btnEl) btnEl.textContent = 'Logout Now';
        banner.classList.remove('gm-wc-warn');
        banner.classList.add('gm-wc-expired');

        var remaining = 5;
        function tick() {
            if (msgEl) {
                msgEl.textContent = '\uD83D\uDD34 Your WalletConnect session has expired. ' +
                    'Logging out in ' + remaining + ' second' + (remaining !== 1 ? 's' : '') + '\u2026';
            }
            if (remaining <= 0) {
                _guardClearWcStorage();
                window.location.href = '/logout';
                return;
            }
            remaining--;
            _autoLogoutTimer = setTimeout(tick, 1000);
        }
        tick();
    }

    function _guardCheck() {
        if (_guardTimer) { clearTimeout(_guardTimer); _guardTimer = null; }

        // Only run for strictly WalletConnect sessions — NOT for injected,
        // manual, or manual_address logins, even if they share the wc-bridge.js file.
        if (_normLogin(_config.loginMethod) !== 'walletconnect') return;

        // Wait until DOM is ready
        if (!document.body) {
            _guardTimer = setTimeout(_guardCheck, 300);
            return;
        }

        var nowSec  = Math.floor(Date.now() / 1000);
        var expiry  = null;
        try {
            var raw = localStorage.getItem('wc_session_data');
            if (raw) {
                var parsed = JSON.parse(raw);
                expiry = (parsed && typeof parsed.expiry === 'number') ? parsed.expiry : null;
            }
        } catch (_) {}

        var storedTopic = localStorage.getItem('wc_session_topic');
        var storedTs    = parseInt(localStorage.getItem('wc_session_timestamp') || '0', 10);
        var tsAgeMs     = storedTs ? (Date.now() - storedTs) : Infinity;
        var maxAgeMs    = 7 * 24 * 60 * 60 * 1000;
        var isOldByTs   = tsAgeMs > maxAgeMs;
        var secsLeft    = expiry ? (expiry - nowSec)
                        : (storedTopic ? (maxAgeMs - tsAgeMs) / 1000 : -1);

        var state = (!storedTopic || isOldByTs || secsLeft <= 0) ? 'expired'
                  : (secsLeft < 24 * 60 * 60)                   ? 'warning'
                  :                                                'ok';

        var banner  = _guardGetOrCreateBanner();
        var msgEl   = document.getElementById(_BANNER_ID + '_msg');
        var btnEl   = document.getElementById(_BANNER_ID + '_btn');
        var xEl     = document.getElementById(_BANNER_ID + '_x');

        banner.classList.remove('gm-wc-warn', 'gm-wc-expired');

        if (state === 'expired') {
            _guardStartAutoLogout(banner, msgEl, btnEl, xEl);
            return;
        }

        if (state === 'warning') {
            var hoursLeft = Math.max(1, Math.round(secsLeft / 3600));
            if (msgEl) msgEl.textContent = '\u26A0\uFE0F Your WalletConnect session expires in ~' +
                hoursLeft + ' hour' + (hoursLeft !== 1 ? 's' : '') +
                '. Reconnect soon to avoid claim failures.';
            if (btnEl) btnEl.textContent = 'Reconnect Now';
            if (xEl)  xEl.style.display = '';
            banner.classList.add('gm-wc-warn');
            _guardTimer = setTimeout(_guardCheck, 30 * 60 * 1000);
            return;
        }

        // state === 'ok': schedule next check just as warning zone begins
        var msUntilWarn = Math.max(0, (secsLeft - 24 * 60 * 60) * 1000);
        _guardTimer = setTimeout(_guardCheck, Math.min(msUntilWarn + 60 * 1000, 60 * 60 * 1000));
    }

    function _startExpiryGuard() {
        // Run after a short delay so the DOM is ready and localStorage is settled
        setTimeout(_guardCheck, 1500);
    }

    // Auto-start when configure() is called — strictly WalletConnect only
    var _origConfigure = configure;
    configure = function (opts) {
        _origConfigure(opts);
        if (_normLogin(_config.loginMethod) === 'walletconnect') _startExpiryGuard();
    };

    global.GMWalletConnect = {
        configure: configure,
        // isPreferred now also recovers pre-existing WalletConnect sessions that
        // were mislabeled as "injected" before login_method was persisted.
        isPreferred: _prefersWcSigning,
        prefersWcSigning: _prefersWcSigning,
        hasStoredSession: _hasStoredWcSessionFor,
        clearStoredSession: _clearStoredSession,
        connect: connect,
        bridgeRequest: bridgeRequest,
        getProvider: getProvider,
        reset: reset,
        isConnected: function () { return !!_state.address; },
        getAddress: function () { return _state.address; }
    };
})(typeof window !== "undefined" ? window : this);
