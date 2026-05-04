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

    var CELO_RPC_URL = "https://forno.celo.org";
    var CELO_CHAIN_HEX = "0xa4ec";
    var CELO_CHAIN_ID = 42220;
    var WC_CDN_URL = "https://cdn.jsdelivr.net/npm/@walletconnect/sign-client@2.17.0/dist/index.umd.js";

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

    function _wcGetClient() {
        if (_state.signClient) return Promise.resolve(_state.signClient);
        if (!_config.projectId) {
            return Promise.reject(new Error("WALLETCONNECT_PROJECT_ID is not configured"));
        }
        return _wcLoadSdk().then(function (SignClient) {
            return SignClient.init({
                projectId: _config.projectId,
                metadata: {
                    name: _config.dappName || "GoodMarket",
                    description: _config.dappDescription || "GoodMarket on Celo",
                    url: _config.dappUrl || (typeof window !== "undefined" ? window.location.origin : ""),
                    icons: [
                        _config.dappIcon ||
                        ((typeof window !== "undefined" ? window.location.origin : "") +
                         "/static/icons/icon-192x192.png" +
                         (_config.assetVersion ? ("?v=" + encodeURIComponent(_config.assetVersion)) : ""))
                    ]
                }
            });
        }).then(function (client) {
            _state.signClient = client;
            try {
                var sessions = client.session && client.session.getAll ? client.session.getAll() : [];
                if (sessions && sessions.length && !_state.browserSession) {
                    _state.browserSession = sessions[sessions.length - 1];
                    _state.mode = "browser";
                    var ns = _state.browserSession.namespaces || {};
                    Object.keys(ns).some(function (key) {
                        var accts = (ns[key] && ns[key].accounts) || [];
                        if (accts.length) {
                            _state.address = String(accts[0]).split(":").pop();
                            return true;
                        }
                        return false;
                    });
                }
            } catch (_) { /* no-op */ }
            return client;
        });
    }

    function _celoJsonRpc(method, params) {
        return fetch(CELO_RPC_URL, {
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

        var modal = document.createElement("div");
        modal.id = "__gmWcModal";
        modal.setAttribute("role", "dialog");
        modal.style.cssText =
            "position:fixed;inset:0;z-index:2147483646;display:flex;align-items:center;" +
            "justify-content:center;background:rgba(2,6,23,0.78);padding:1rem;";

        var card = document.createElement("div");
        card.style.cssText =
            "background:#0f172a;border:1px solid rgba(124,58,237,0.45);border-radius:18px;" +
            "padding:1.4rem 1.4rem 1.2rem;max-width:340px;width:100%;text-align:center;color:#f8fafc;" +
            "font-family:inherit;box-shadow:0 24px 60px rgba(15,23,42,0.6);";

        var title = document.createElement("div");
        title.textContent = label || "Approve in your wallet";
        title.style.cssText = "font-weight:700;font-size:1rem;margin-bottom:0.3rem;color:#fbbf24;";

        var sub = document.createElement("div");
        sub.textContent = "Scan this QR with your WalletConnect-enabled wallet (MetaMask, Trust, Valora, etc.).";
        sub.style.cssText = "font-size:0.78rem;line-height:1.4;color:rgba(248,250,252,0.7);margin-bottom:0.9rem;";

        var img = document.createElement("img");
        img.alt = "WalletConnect QR";
        img.width = 200;
        img.height = 200;
        img.src = "https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=" + encodeURIComponent(uri);
        img.style.cssText = "background:#fff;padding:8px;border-radius:12px;display:block;margin:0 auto 0.9rem;";

        var copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.textContent = "📋 Copy URI";
        copyBtn.style.cssText =
            "padding:0.55rem 0.9rem;border-radius:10px;border:none;background:#6366f1;color:#fff;" +
            "font-weight:600;font-size:0.85rem;cursor:pointer;margin-right:0.4rem;";
        copyBtn.addEventListener("click", function () {
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(uri);
                } else {
                    var ta = document.createElement("textarea");
                    ta.value = uri;
                    ta.style.position = "fixed"; ta.style.left = "-9999px";
                    document.body.appendChild(ta); ta.select();
                    document.execCommand("copy"); document.body.removeChild(ta);
                }
                copyBtn.textContent = "✅ Copied!";
                setTimeout(function () { copyBtn.textContent = "📋 Copy URI"; }, 1600);
            } catch (_) { /* no-op */ }
        });

        var cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.textContent = "Close";
        cancelBtn.style.cssText =
            "padding:0.55rem 0.9rem;border-radius:10px;border:1px solid rgba(255,255,255,0.18);" +
            "background:rgba(255,255,255,0.06);color:#f8fafc;font-weight:600;font-size:0.85rem;cursor:pointer;";
        cancelBtn.addEventListener("click", _defaultHideQr);

        card.appendChild(title);
        card.appendChild(sub);
        card.appendChild(img);
        card.appendChild(copyBtn);
        card.appendChild(cancelBtn);
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
        if (_state.address) return Promise.resolve(_state.address);
        var wantedAddress = String(_config.walletAddress || "").toLowerCase();

        // Try the Node sidecar first when enabled (matches homepage login flow).
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

                        var deadline = Date.now() + 120000;
                        function poll() {
                            if (Date.now() >= deadline) {
                                throw new Error("WalletConnect approval timed out.");
                            }
                            return _delay(2000)
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
                        // Sidecar unavailable / timed out — fall back to in-browser WC SDK.
                        try { _config.log("wc-bridge sidecar fallback:", err && err.message); } catch (_) {}
                        return null;
                    });
            })();
        }

        return sidecarPromise.then(function (addr) {
            if (addr) return addr;
            return _wcGetClient().then(function (client) {
                if (_state.browserSession && _state.address) {
                    _state.mode = "browser";
                    return _state.address;
                }
                return client.connect({
                    requiredNamespaces: {},
                    optionalNamespaces: {
                        eip155: {
                            methods: [
                                "eth_accounts",
                                "eth_sendTransaction",
                                "eth_getTransactionReceipt",
                                "eth_chainId",
                                "personal_sign",
                                "eth_sign",
                                "eth_signTypedData",
                                "eth_signTypedData_v4"
                            ],
                            chains: ["eip155:" + CELO_CHAIN_ID],
                            events: ["chainChanged", "accountsChanged"]
                        }
                    }
                }).then(function (result) {
                    _state.mode = "browser";
                    if (result && result.uri) {
                        _defaultShowQr(result.uri, "Approve in your wallet");
                    }
                    return result.approval();
                }).then(function (session) {
                    _defaultHideQr();
                    _state.browserSession = session;
                    var ns = session.namespaces || {};
                    Object.keys(ns).some(function (key) {
                        var accts = (ns[key] && ns[key].accounts) || [];
                        if (accts.length) {
                            _state.address = String(accts[0]).split(":").pop();
                            return true;
                        }
                        return false;
                    });
                    if (!_state.address) {
                        throw new Error("No accounts returned from wallet");
                    }
                    return _state.address;
                }, function (err) {
                    _defaultHideQr();
                    throw err;
                });
            });
        }).then(function (addr) {
            if (wantedAddress && addr && String(addr).toLowerCase() !== wantedAddress) {
                // Force a fresh session next time so user can re-approve.
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
            return connect().then(function (addr) { return [addr]; });
        }
        if (method === "eth_chainId") {
            return Promise.resolve(CELO_CHAIN_HEX);
        }
        if (method === "net_version") {
            return Promise.resolve(String(CELO_CHAIN_ID));
        }
        if (method === "wallet_switchEthereumChain" || method === "wallet_addEthereumChain") {
            // Sessions are scoped to Celo, so these are no-ops over the bridge.
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
                    });
                }
                // Fall through to the in-browser SignClient session.
                if (!_state.browserSession) {
                    throw _wcRpcError("WalletConnect browser session is not active.", null, -32603);
                }
                return _wcGetClient().then(function (client) {
                    return client.request({
                        topic: _state.browserSession.topic,
                        chainId: "eip155:" + CELO_CHAIN_ID,
                        request: { method: method, params: p }
                    }).catch(function (wcErr) {
                        // Re-throw with .code preserved so ethers.js sees a
                        // proper EIP-1193 error (4001 → user rejected, etc.)
                        // instead of wrapping it as "could not coalesce error".
                        var code = (wcErr && typeof wcErr.code === "number") ? wcErr.code : -32603;
                        var msg = (wcErr && wcErr.message) ? String(wcErr.message) : "WalletConnect request failed";
                        throw _wcRpcError(msg, msg, code);
                    });
                });
            });
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
