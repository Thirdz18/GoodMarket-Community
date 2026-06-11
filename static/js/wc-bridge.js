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

    // Supported networks for WalletConnect chain switching
    var SUPPORTED_NETWORKS = {
        "0xa4ec": { // Celo Mainnet
            name: "Celo",
            chainId: 42220,
            rpc: "https://forno.celo.org",
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

    function _normLogin(method) {
        return String(method == null ? "" : method).toLowerCase();
    }

    function _shouldPrefer() {
        return ["walletconnect", "manual", "manual_address"].indexOf(_normLogin(_config.loginMethod)) >= 0;
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

            // ── Step 1: Poll IndexedDB-backed sessions (primary source) ─────────
            // We must NOT call getActiveSessions() immediately after init() — the
            // WalletConnect SDK reads from IndexedDB asynchronously. Polling for
            // up to 3 s gives it enough time on low-end Android devices.
            return _pollForSessions(client, 3000, 300).then(function (liveSession) {
                if (liveSession) {
                    _state.browserSession = liveSession;
                    _state.address = _addrFromSession(liveSession);
                    try { _config.log("[wc-bridge] Restored live WC session from SDK storage:", liveSession.topic); } catch (_) {}
                    return client;
                }

                // ── Step 2: localStorage backup (written by homepage at login) ──
                // Only used when the SDK's own IndexedDB has no live session —
                // e.g. after a hard refresh that cleared IndexedDB, or on a device
                // where IndexedDB is sandboxed per-tab.
                // IMPORTANT: we do NOT call client.session.set() here. That internal
                // API injects session metadata into the client's in-memory map but
                // does NOT re-establish the relay WebSocket subscription, so
                // client.request({ topic }) would silently time-out. Instead we
                // store the session metadata on _state so that bridgeRequest() can
                // attempt the relay call and surface a clear "session expired" error
                // if the relay rejects the topic.
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
                                // Attempt to register the session into the client's internal
                                // session store AND subscribe to the topic on the relay. The
                                // SDK exposes client.core.relayer.subscribe(topic) for this
                                // purpose; fall back silently if the API is not available.
                                try {
                                    if (client.session && typeof client.session.set === 'function') {
                                        client.session.set(parsedSession.topic, parsedSession);
                                    }
                                } catch (_) { /* internal API — ignore */ }
                                try {
                                    if (client.core && client.core.relayer &&
                                        typeof client.core.relayer.subscribe === 'function') {
                                        client.core.relayer.subscribe(parsedSession.topic);
                                    }
                                } catch (_) { /* optional — ignore */ }

                                _state.browserSession = parsedSession;
                                _state.address = storedAddress;
                                try { _config.log("[wc-bridge] Restored WC session from localStorage backup:", storedTopic); } catch (_) {}
                            }
                        } catch (parseErr) { /* ignore */ }
                    }

                    if (storedTopic && (sessionTooOld || !storedSessionData)) {
                        try {
                            localStorage.removeItem('wc_session_topic');
                            localStorage.removeItem('wc_session_address');
                            localStorage.removeItem('wc_session_data');
                            localStorage.removeItem('wc_session_timestamp');
                        } catch (_) {}
                    }
                } catch (lsErr) { /* ignore — Safari private mode etc. */ }

                return client;
            });
        });
    }

    function _celoJsonRpc(method, params) {
        return fetch((_config.rpcUrl || DEFAULT_RPC_URL), {
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
            if (data.error) throw new Error(data.error.message || "RPC error");
            return data.result;
        });
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
                // Fall through to the in-browser SignClient session.
                // _wcGetClient() already runs the full session-restore logic
                // (IndexedDB poll → localStorage backup), so by the time we
                // reach this block _state.browserSession should already be
                // populated if a valid session exists.
                return _wcGetClient().then(function (client) {
                    // If session was not restored during _wcGetClient (possible
                    // if configure() was called after the first _wcGetClient run),
                    // do one last poll against the live SignClient session map.
                    if (!_state.browserSession) {
                        return _pollForSessions(client, 2000, 300).then(function (liveSession) {
                            if (liveSession) {
                                _state.browserSession = liveSession;
                                _state.address = _addrFromSession(liveSession);
                            }
                            return client;
                        });
                    }
                    return client;
                }).then(function (client) {
                    // If we have a session, use it
                    if (_state.browserSession) {
                        return _doWcRequest(client, method, p).catch(function (wcErr) {
                            var errMsg = (wcErr && wcErr.message) ? String(wcErr.message) : "";
                            // Session was rejected or expired by the relay — clear it so
                            // the next action triggers a fresh re-authenticate flow rather
                            // than looping on the same dead session.
                            if (/unknown connector|session.*not found|no matching key|expired/i.test(errMsg)) {
                                _state.browserSession = null;
                                try {
                                    localStorage.removeItem('wc_session_topic');
                                    localStorage.removeItem('wc_session_address');
                                    localStorage.removeItem('wc_session_data');
                                    localStorage.removeItem('wc_session_timestamp');
                                } catch (_) {}
                                throw _wcRpcError(
                                    "Your WalletConnect session has expired. Please log out and log in again to reconnect your wallet.",
                                    null, -32603
                                );
                            }
                            var code = (wcErr && typeof wcErr.code === "number") ? wcErr.code : -32603;
                            throw _wcRpcError(errMsg || "WalletConnect request failed", errMsg, code);
                        });
                    }
                    
                    // No session available - fail gracefully without showing QR
                    // Transaction signing should never require user to scan new QR.
                    // Clear any stale localStorage so re-login starts clean.
                    try {
                        localStorage.removeItem('wc_session_topic');
                        localStorage.removeItem('wc_session_address');
                        localStorage.removeItem('wc_session_data');
                        localStorage.removeItem('wc_session_timestamp');
                    } catch (_) {}
                    throw _wcRpcError("No active WalletConnect session. Please log out and log in again to reconnect your wallet.", null, -32603);
                });
            });
        }

        // Helper to make WC request - extracted for reuse on reconnect
        function _doWcRequest(client, method, p) {
            // Wrap the request with a timeout to prevent the wallet
            // (MetaMask Mobile in particular) from hanging
            // indefinitely. The wallet sometimes silently drops
            // requests so we surface a clear failure after 45s.
            var requestPromise = client.request({
                topic: _state.browserSession.topic,
                chainId: "eip155:" + Number(_config.chainId || DEFAULT_CHAIN_ID),
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

    global.GMWalletConnect = {
        configure: configure,
        isPreferred: _shouldPrefer,
        connect: connect,
        bridgeRequest: bridgeRequest,
        getProvider: getProvider,
        reset: reset,
        isConnected: function () { return !!_state.address; },
        getAddress: function () { return _state.address; }
    };
})(typeof window !== "undefined" ? window : this);
