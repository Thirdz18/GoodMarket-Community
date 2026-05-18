/* GoodMarket transaction-error formatter
 *
 * Frontend pages call ethers.js / WalletConnect / MiniPay through a wide
 * variety of paths, and each of them surfaces failures with a different
 * shape. ethers v6 in particular wraps EIP-1193 errors as
 *   "could not coalesce error (error={…},payload={…},code=UNKNOWN_ERROR,…)"
 * which is fine for debugging but is hostile UX when shown directly to a
 * non-developer.
 *
 * `GMTxError.format(err)` walks the (possibly nested) error and returns a
 * short, human-readable message that's safe to drop into an alert string
 * with no JSON / dev-only fields leaking through.
 *
 * The detector covers:
 *   - User cancellations (EIP-1193 4001, WalletConnect 5000/5001/-32603,
 *     "user rejected", "user denied", "user disapproved", "request rejected",
 *     etc.)
 *   - Insufficient balance / funds
 *   - Gas estimation / out-of-gas
 *   - Network / RPC connectivity
 *   - Approval-required hints
 *   - Generic fallback that strips ethers.js boilerplate
 *
 * No external dependencies — load before any caller via
 *   <script src="/static/js/tx-error.js?v={{ ASSET_VERSION }}"></script>
 */
(function (global) {
    "use strict";

    if (global.GMTxError && typeof global.GMTxError.format === "function") return;

    // Wrap a thrown Error so format() will pass `.message` through verbatim
    // (instead of running it through the wallet/RPC pattern detector and
    // replacing it with a generic "Insufficient funds for gas" string).
    // Use this for preflight errors where the caller already produced a
    // user-facing message.
    function asFriendly(err) {
        if (err && typeof err === "object") {
            try { err._gmFriendly = true; } catch (_) {}
        }
        return err;
    }

    function _stringify(value) {
        if (value == null) return "";
        if (typeof value === "string") return value;
        try { return JSON.stringify(value); } catch (_) {}
        try { return String(value); } catch (_) {}
        return "";
    }

    // Walk up to a few levels deep, collecting every plausible message /
    // code / data field. Some errors stack `cause` / `info` / `error`
    // several layers deep (MetaMask → ethers.js → BrowserProvider).
    function _spelunk(err) {
        var out = { code: undefined, msg: "", joined: "" };
        if (!err) return out;
        var seen = new Set();
        var stack = [err];
        var pieces = [];
        while (stack.length) {
            var cur = stack.shift();
            if (!cur || typeof cur !== "object" || seen.has(cur)) continue;
            seen.add(cur);
            if (out.code === undefined && cur.code !== undefined) out.code = cur.code;
            if (typeof cur.shortMessage === "string") pieces.push(cur.shortMessage);
            if (typeof cur.reason === "string") pieces.push(cur.reason);
            if (typeof cur.message === "string") pieces.push(cur.message);
            if (typeof cur.data === "string") pieces.push(cur.data);
            if (cur.error) stack.push(cur.error);
            if (cur.cause) stack.push(cur.cause);
            if (cur.info) stack.push(cur.info);
            if (cur.data && typeof cur.data === "object") stack.push(cur.data);
            if (cur.payload && typeof cur.payload === "object") stack.push(cur.payload);
        }
        out.msg = pieces.length ? pieces[0] : "";
        out.joined = pieces.join(" \n ");
        return out;
    }

    function _isUserRejection(code, joined) {
        if (code === 4001 || code === 4100 || code === 4200) return true; // EIP-1193
        if (code === 5000 || code === 5001 || code === 5002) return true; // WalletConnect "user disapproved"
        if (code === "ACTION_REJECTED") return true; // ethers v6 explicit code
        var rx = /user rejected|user denied|user disapproved|user cancel|request[ _]?rejected|rejected by user|action_rejected|user closed|user dismissed|reject(ed)? the request|you rejected|signature was rejected|approval rejected|transaction rejected|denied transaction signature/i;
        return rx.test(joined);
    }

    function _isInsufficientFunds(joined) {
        return /insufficient funds|insufficient balance|exceeds balance|not enough .*(funds|balance|gas)/i.test(joined);
    }

    function _isGasIssue(joined) {
        return /out of gas|intrinsic gas too low|gas required exceeds|exceeds block gas limit/i.test(joined);
    }

    function _isAllowanceIssue(joined) {
        return /erc20: insufficient allowance|allowance|stf|transferhelper|transfer_from_failed|safetransferfrom/i.test(joined);
    }

    function _isReverted(joined) {
        return /execution reverted|call exception|transaction reverted/i.test(joined);
    }

    function _isNetworkIssue(joined) {
        return /failed to fetch|network error|networkerror|timeout|timed out|fetch failed|err_network|err_internet|connection refused|aborted/i.test(joined);
    }

    function _isWcSessionIssue(joined) {
        return /walletconnect.*(unavailable|not active|approval timed out|expired|not configured)|wc session|session not (found|established)/i.test(joined);
    }

    function _isUnsupportedRpcMethod(joined) {
        // Mobile wallets that can't service ethers' read-only preflight RPCs
        // typically reply with "Missing or invalid. request() method: …"
        // (Trust, MiniPay, etc.). Surface a non-technical retry hint.
        return /missing or invalid\.? request\(\) method|method not supported|method not found|unsupported method/i.test(joined);
    }

    function _isQuoteIssue(joined) {
        return /allowance too low.*approval|approval may not have been confirmed/i.test(joined);
    }

    function _stripEthersBoilerplate(s) {
        if (!s) return "";
        return String(s)
            .replace(/\(error=\{[\s\S]*?\}(,|\))/g, "")
            .replace(/\(payload=\{[\s\S]*?\}(,|\))/g, "")
            .replace(/\(action="[^"]+",?/g, "")
            .replace(/\(transaction=\{[\s\S]*?\}(,|\))/g, "")
            .replace(/\(reason=null,?/g, "")
            .replace(/\bcode=[A-Z_]+,?/g, "")
            .replace(/\bversion=[\d.]+\)?/g, "")
            .replace(/[\(\),]+\s*$/g, "")
            .replace(/\s{2,}/g, " ")
            .replace(/^\s*[:\-]\s*/, "")
            .trim();
    }

    function _capitalize(s) {
        if (!s) return s;
        return s.charAt(0).toUpperCase() + s.slice(1);
    }

    function _truncate(s, n) {
        n = n || 180;
        if (!s) return s;
        return s.length > n ? s.slice(0, n - 1).trim() + "…" : s;
    }

    function format(err) {
        if (err == null) return "Unknown error";
        if (typeof err === "string") return _truncate(_stripEthersBoilerplate(err)) || "Unknown error";

        // Preflight errors raised by app code with `_gmFriendly = true`
        // already contain a user-facing, fully-itemized message — pass them
        // through verbatim instead of crushing them into a generic
        // "Insufficient CELO for gas fees" string.
        if (err && err._gmFriendly === true && typeof err.message === "string" && err.message) {
            return _truncate(err.message, 280);
        }

        var info = _spelunk(err);
        var msg = info.msg || "";
        var joined = info.joined || "";
        var code = info.code;

        if (_isUserRejection(code, joined)) return "Transaction cancelled in your wallet.";
        if (_isUnsupportedRpcMethod(joined)) return "Your wallet couldn't process this request. Please retry, or reconnect WalletConnect from the homepage.";
        if (_isWcSessionIssue(joined)) {
            if (/timed out|expired/i.test(joined)) return "WalletConnect approval timed out. Please try again.";
            if (/not configured/i.test(joined)) return "WalletConnect is not configured on this server.";
            return "WalletConnect session is not active. Please reconnect and try again.";
        }
        if (_isInsufficientFunds(joined)) {
            if (/celo|gas/i.test(joined)) {
                // The wallet RPC reports "insufficient funds" without naming
                // the asset. On Celo this is ambiguous: regular wallets pay
                // gas in CELO, but MiniPay pays gas in stablecoin (cUSD /
                // USDT / USDC) via CIP-64. Mention both so the message is
                // accurate either way and points to the cUSD-faucet recovery
                // path the swap pages now offer automatically.
                return "Insufficient gas balance. MiniPay pays gas in cUSD/USDT/USDC; other wallets pay in CELO. Top up the right asset and try again.";
            }
            return "Insufficient balance for this transaction.";
        }
        if (_isAllowanceIssue(joined)) return "Token approval is missing or too low. Please re-approve and try again.";
        if (_isGasIssue(joined)) return "Transaction needs more gas. Please try again or contact support.";
        if (_isNetworkIssue(joined)) return "Network error. Please check your connection and try again.";
        if (_isReverted(joined)) return "Transaction was rejected on-chain. Please check the inputs and try again.";

        // Generic ethers wrapper — try to recover the inner message
        if (/could not coalesce error/i.test(msg)) {
            var inner = "";
            // Pluck the first useful inner message we already collected.
            var parts = (joined || "").split(" \n ");
            for (var i = 0; i < parts.length; i++) {
                var p = parts[i];
                if (!p || /could not coalesce error/i.test(p)) continue;
                inner = p; break;
            }
            if (inner) return _truncate(_stripEthersBoilerplate(inner)) || "Transaction failed. Please try again.";
            return "Transaction failed. Please try again.";
        }

        var clean = _stripEthersBoilerplate(msg);
        if (!clean) return "Transaction failed. Please try again.";
        return _truncate(_capitalize(clean));
    }

    function isUserRejection(err) {
        if (err == null) return false;
        var info = _spelunk(err);
        return _isUserRejection(info.code, info.joined || "");
    }

    global.GMTxError = { format: format, isUserRejection: isUserRejection, asFriendly: asFriendly };
})(typeof window !== "undefined" ? window : globalThis);
