/*
 * minipay-gas-topup.js
 *
 * Auto-detect + auto-prompt UX for MiniPay users who hold CELO but no
 * stablecoin balance (cUSD / USDT / USDC). MiniPay pays gas only in
 * stablecoins, so a CELO-only user gets stuck on every on-chain action
 * (claim, swap, send, bridge, etc.).
 *
 * Flow:
 *   1) Detect MiniPay balances and whether stablecoin gas is missing.
 *   2) Request the GoodDollar CELO faucet best-effort when CELO is low.
 *   3) Always allow the direct cUSD gas faucet for wallets missing stablecoin gas.
 *   4) If extra CELO is available, optionally swap it to cUSD, then resume.
 *      CELO -> cUSD swaps can opt out of this extra auto-swap while still
 *      receiving the faucet cUSD needed to pay MiniPay gas.
 *
 * Constraint: every on-chain tx still requires user signature inside
 * MiniPay, so this is "auto-prompt + chained txs", not "zero-tap auto".
 *
 * Public API (window.MPGasTopUp):
 *   - isMiniPay()                            -> bool
 *   - getBalances(walletAddr)                -> { celo, cusd, usdt, usdc } as bigint
 *   - needsTopUpFromBalances(balances)       -> bool
 *   - ensureToppedUp(walletAddr, opts)       -> Promise<{ proceed, swapped?, stableGasOnly?, cancelled?, error? }>
 *   - runWithGasTopUp(walletAddr, fn, opts)  -> Promise<{ ranAction, ... }>
 *   - maybeShowBanner(walletAddr, opts)      -> Promise<bool>
 */
(function (global) {
    'use strict';
    if (global.MPGasTopUp) return;

    // ─── Constants ────────────────────────────────────────────────────────
    // Celo mainnet token addresses
    const CELO = '0x471EcE3750Da237f93B8E339c536989b8978a438';
    const CUSD = '0x765DE816845861e75A25fCA122bb6898B8B1282a';
    const USDT = '0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e';
    const USDC = '0xcebA9300f2b948710d2653dD7B07f33A8B32118C';

    // Uniswap V3 SwapRouter02 on Celo (same as templates/swap.html)
    const UNISWAP_ROUTER = '0x5615CDAb10dc425a742d643d949a7F474C01abc4';
    // CELO/cUSD pool fee tiers to try, deepest-first.
    const FEE_TIERS = [3000, 500, 10000, 100];

    const ETHERS_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/ethers/6.13.4/ethers.umd.min.js';

    // MiniPay gas is paid from stablecoins, not CELO. When a MiniPay user
    // has CELO but no stablecoin gas budget, convert every spendable CELO
    // unit above this reserve so the wallet keeps a small native balance.
    const CELO_RESERVE_AFTER_TOPUP_WEI = 90000000000000000n; // 0.09 CELO
    const CELO_RESERVE_AFTER_TOPUP_STR = '0.09';

    // MiniPay needs a small stablecoin balance to pay fee-currency gas.
    // Tuned to match the server-side MINIPAY_STABLECOIN_MIN_USD threshold.
    // Keep this threshold below the server faucet amount so one refill can
    // clear it. The faucet display amount mirrors the backend default and is
    // sized for the approve + swap path needed by CELO-only MiniPay users.
    const STABLECOIN_GAS_MIN_USD = 0.01;
    const CUSD_FAUCET_DISPLAY_AMOUNT = '0.05';
    // Match the backend FAUCET_MIN_CELO default. MiniPay still pays claim gas
    // in stablecoins, so the CELO faucet is a best-effort recovery path for
    // wallets below this floor; it must not block the cUSD gas budget.
    const CELO_FAUCET_TRIGGER_BELOW_WEI = 100000000000000000n; // 0.1 CELO
    const CELO_FAUCET_TRIGGER_BELOW_STR = '0.1';
    const CUSD_FAUCET_ENDPOINT = '/api/minipay/stablecoin-faucet';
    const CELO_FAUCET_ENDPOINT = '/api/faucet/gas';
    const CUSD_FAUCET_PROGRAM_LABEL = 'Program by Betz & Omar Team';

    // Stablecoin "dust" threshold below which we treat the user as having
    // effectively zero stablecoin gas budget. Approx $0.005.
    const STABLECOIN_DUST_USD = 0.005;

    // ─── ethers.js dynamic loader (idempotent) ────────────────────────────
    let _ethersPromise = null;
    function _loadEthers() {
        if (global.ethers) return Promise.resolve(global.ethers);
        if (_ethersPromise) return _ethersPromise;
        _ethersPromise = new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = ETHERS_CDN;
            s.crossOrigin = 'anonymous';
            s.referrerPolicy = 'no-referrer';
            s.onload = () => global.ethers
                ? resolve(global.ethers)
                : reject(new Error('ethers loaded but window.ethers undefined'));
            s.onerror = () => reject(new Error('ethers script load error'));
            document.head.appendChild(s);
        });
        return _ethersPromise;
    }

    // ─── MiniPay detection ────────────────────────────────────────────────
    // Check multiple signals to detect MiniPay: provider flag, user agent,
    // and Opera Mini context (MiniPay runs inside Opera's mini-app shell).
    // We cache the result after the first positive detection because provider
    // injection can be delayed and we don't want flaky detection mid-flow.
    let _miniPayDetectedCache = null;
    function _isMiniPay() {
        // Return cached result if we already detected MiniPay
        if (_miniPayDetectedCache === true) return true;
        
        try {
            const eth = global.ethereum;
            if (eth && eth.isMiniPay) {
                _miniPayDetectedCache = true;
                return true;
            }
            if (eth && Array.isArray(eth.providers)
                && eth.providers.some(p => p && p.isMiniPay)) {
                _miniPayDetectedCache = true;
                return true;
            }
            // Check user agent for MiniPay or Opera Mini context
            if (typeof navigator !== 'undefined') {
                const ua = navigator.userAgent || '';
                if (/minipay/i.test(ua) || /OPR.*Mini/i.test(ua) || /Opera.*Mini/i.test(ua)) {
                    _miniPayDetectedCache = true;
                    return true;
                }
            }
        } catch (_) { /* swallow */ }
        return false;
    }
    
    // Force MiniPay detection to be set (used by callers that know they're in MiniPay)
    function _setMiniPayDetected() {
        _miniPayDetectedCache = true;
    }

    function _getProvider() {
        try {
            const eth = global.ethereum;
            if (!eth) return null;
            if (eth.isMiniPay) return eth;
            if (Array.isArray(eth.providers)) {
                const mp = eth.providers.find(p => p && p.isMiniPay);
                if (mp) return mp;
            }
            return eth;
        } catch (_) { return null; }
    }

    // ─── Balance reads (raw eth_call/eth_getBalance, no ethers needed) ────
    async function _ethCall(provider, to, data) {
        return await provider.request({
            method: 'eth_call',
            params: [{ to: to, data: data }, 'latest'],
        });
    }

    async function _getCeloBalance(provider, walletAddr) {
        try {
            const hex = await provider.request({
                method: 'eth_getBalance',
                params: [walletAddr, 'latest'],
            });
            return BigInt(hex || '0x0');
        } catch (_) { return 0n; }
    }

    function _erc20BalanceOfCalldata(walletAddr) {
        return '0x70a08231'
            + walletAddr.toLowerCase().replace('0x', '').padStart(64, '0');
    }

    async function _getErc20Balance(provider, tokenAddr, walletAddr) {
        try {
            const data = _erc20BalanceOfCalldata(walletAddr);
            const res = await _ethCall(provider, tokenAddr, data);
            return BigInt(res || '0x0');
        } catch (_) { return 0n; }
    }

    async function getBalances(walletAddr, providerOpt) {
        const provider = providerOpt || _getProvider();
        if (!provider || !walletAddr) {
            return { celo: 0n, cusd: 0n, usdt: 0n, usdc: 0n };
        }
        const [celo, cusd, usdt, usdc] = await Promise.all([
            _getCeloBalance(provider, walletAddr),
            _getErc20Balance(provider, CUSD, walletAddr),
            _getErc20Balance(provider, USDT, walletAddr),
            _getErc20Balance(provider, USDC, walletAddr),
        ]);
        return { celo: celo, cusd: cusd, usdt: usdt, usdc: usdc };
    }

    function _stablecoinUsdTotal(balances) {
        if (!balances) return 0;
        return (Number(balances.cusd || 0n) / 1e18)
            + (Number(balances.usdt || 0n) / 1e6)
            + (Number(balances.usdc || 0n) / 1e6);
    }

    function hasStablecoinGasBalance(balances) {
        return _stablecoinUsdTotal(balances) >= STABLECOIN_GAS_MIN_USD;
    }

    function getAutoSwapAmountWei(balances) {
        if (!balances || !balances.celo || balances.celo <= CELO_RESERVE_AFTER_TOPUP_WEI) {
            return 0n;
        }
        return balances.celo - CELO_RESERVE_AFTER_TOPUP_WEI;
    }

    function isBelowCeloFaucetFloor(balances) {
        return !balances || !balances.celo || balances.celo < CELO_FAUCET_TRIGGER_BELOW_WEI;
    }

    function needsTopUpFromBalances(balances) {
        return getAutoSwapAmountWei(balances) > 0n && !hasStablecoinGasBalance(balances);
    }

    // ─── UI: styles + modals + banner ─────────────────────────────────────
    function _injectStyles() {
        if (document.getElementById('mp-gtu-styles')) return;
        const style = document.createElement('style');
        style.id = 'mp-gtu-styles';
        style.textContent = ''
            + '.mp-gtu-overlay{position:fixed;inset:0;background:rgba(0,0,0,.78);'
            + 'z-index:99999;display:flex;align-items:center;justify-content:center;'
            + 'padding:1rem;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;}'
            + '.mp-gtu-card{background:#1a1f2e;color:#fff;border-radius:16px;'
            + 'padding:1.4rem 1.4rem 1.2rem;max-width:380px;width:100%;'
            + 'box-shadow:0 24px 60px rgba(0,0,0,.5);'
            + 'border:1px solid rgba(255,255,255,.1);}'
            + '.mp-gtu-title{font-size:1.05rem;font-weight:700;margin-bottom:.5rem;}'
            + '.mp-gtu-body{font-size:.9rem;line-height:1.5;color:#cbd5e1;}'
            + '.mp-gtu-actions{display:flex;gap:.5rem;margin-top:1.1rem;}'
            + '.mp-gtu-btn{flex:1;padding:.65rem .8rem;border-radius:10px;'
            + 'border:none;cursor:pointer;font-size:.9rem;font-weight:600;}'
            + '.mp-gtu-btn-primary{background:linear-gradient(135deg,#7c3aed,#6d28d9);color:#fff;}'
            + '.mp-gtu-btn-primary:disabled{opacity:.6;cursor:not-allowed;}'
            + '.mp-gtu-btn-secondary{background:rgba(255,255,255,.08);color:#cbd5e1;}'
            + '.mp-gtu-status{margin-top:.85rem;padding:.55rem .75rem;'
            + 'background:rgba(255,255,255,.04);border-radius:8px;font-size:.8rem;'
            + 'color:#94a3b8;line-height:1.45;}'
            + '.mp-gtu-program{margin-top:.75rem;font-size:.72rem;font-weight:700;'
            + 'letter-spacing:.04em;text-transform:uppercase;color:#fbbf24;}'
            + '.mp-gtu-banner{margin:0 auto 1rem;max-width:600px;'
            + 'background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.4);'
            + 'border-radius:12px;padding:.8rem 1rem;color:#fbbf24;'
            + 'font-size:.85rem;display:flex;gap:.7rem;align-items:center;'
            + 'flex-wrap:wrap;}'
            + '.mp-gtu-banner-text{flex:1;min-width:200px;line-height:1.4;}'
            + '.mp-gtu-banner-btn{background:linear-gradient(135deg,#7c3aed,#6d28d9);'
            + 'color:#fff;border:none;border-radius:8px;padding:.45rem .9rem;'
            + 'cursor:pointer;font-size:.82rem;font-weight:600;white-space:nowrap;}'
            + '.mp-gtu-toast{position:fixed;left:50%;top:calc(1rem + env(safe-area-inset-top,0px));'
            + 'transform:translateX(-50%);z-index:100000;max-width:min(92vw,420px);'
            + 'background:#06281f;color:#dcfce7;border:1px solid rgba(34,197,94,.45);'
            + 'box-shadow:0 16px 40px rgba(0,0,0,.35);border-radius:14px;'
            + 'padding:.85rem 1rem;font-size:.86rem;line-height:1.45;font-weight:600;'
            + 'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;text-align:left;}';
        document.head.appendChild(style);
    }

    function _formatCelo(weiBig) {
        try {
            const num = Number(weiBig) / 1e18;
            return num.toFixed(4);
        } catch (_) { return '?'; }
    }

    // Humanize remaining cooldown seconds for user-facing copy. Server returns
    // raw seconds in `recent_refill_cooldown_seconds`; raw "129600s" is useless
    // to a user, "~36 hours" is actionable.
    function _humanizeCooldownSeconds(secs) {
        const n = Number(secs);
        if (!n || n <= 0 || !isFinite(n)) return null;
        if (n < 60) return Math.max(1, Math.round(n)) + 's';
        if (n < 3600) return Math.round(n / 60) + ' min';
        if (n < 86400) {
            const hours = Math.round(n / 3600);
            return hours + (hours === 1 ? ' hour' : ' hours');
        }
        const days = Math.round(n / 86400);
        return days + (days === 1 ? ' day' : ' days');
    }

    // Return a localized "ready at" timestamp for the cooldown copy, e.g.
    // "May 9, 10:30 AM". Best-effort — falls back to null on Intl errors.
    function _formatCooldownReadyAt(secs) {
        const n = Number(secs);
        if (!n || n <= 0 || !isFinite(n)) return null;
        try {
            const d = new Date(Date.now() + (n * 1000));
            return d.toLocaleString(undefined, {
                hour: '2-digit', minute: '2-digit',
                month: 'short', day: 'numeric',
            });
        } catch (_) {
            return null;
        }
    }

    function _showConfirmModal(opts) {
        _injectStyles();
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'mp-gtu-overlay';
            overlay.innerHTML = ''
                + '<div class="mp-gtu-card" role="dialog" aria-modal="true">'
                + '<div class="mp-gtu-title">⛽ Confirm swap to claim G$</div>'
                + '<div class="mp-gtu-body">' + (opts.body || '') + '</div>'
                + '<div class="mp-gtu-status">'
                + 'Convert <strong>~' + opts.amountCelo + ' CELO</strong> → <strong>cUSD</strong> now? '
                + 'Current CELO balance: ' + opts.celoFmt + '. We\'ll leave about ' + (opts.reserveCelo || CELO_RESERVE_AFTER_TOPUP_STR) + ' CELO untouched as your MiniPay reserve.'
                + '</div>'
                + '<div class="mp-gtu-actions">'
                + '<button class="mp-gtu-btn mp-gtu-btn-secondary" data-action="cancel">Cancel</button>'
                + '<button class="mp-gtu-btn mp-gtu-btn-primary" data-action="confirm">Convert now</button>'
                + '</div></div>';
            document.body.appendChild(overlay);
            const cleanup = (val) => {
                if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
                resolve(val);
            };
            overlay.querySelector('[data-action="cancel"]')
                .addEventListener('click', () => cleanup(false));
            overlay.querySelector('[data-action="confirm"]')
                .addEventListener('click', () => cleanup(true));
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) cleanup(false);
            });
        });
    }

    function _showProgressModal(initialText, title) {
        _injectStyles();
        const overlay = document.createElement('div');
        overlay.className = 'mp-gtu-overlay';
        overlay.innerHTML = ''
            + '<div class="mp-gtu-card">'
            + '<div class="mp-gtu-title">' + (title || '⛽ Converting CELO → cUSD') + '</div>'
            + '<div class="mp-gtu-status" id="mp-gtu-progress-text">'
            + (initialText || 'Preparing swap…') + '</div>'
            + (title && /stablecoin gas/i.test(title) ? '<div class="mp-gtu-program">' + CUSD_FAUCET_PROGRAM_LABEL + '</div>' : '')
            + '<div class="mp-gtu-actions">'
            + '<button class="mp-gtu-btn mp-gtu-btn-secondary" data-action="hide">Hide</button>'
            + '</div></div>';
        document.body.appendChild(overlay);
        overlay.querySelector('[data-action="hide"]').addEventListener('click', () => {
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        });
        return {
            update: (text) => {
                const el = overlay.querySelector('#mp-gtu-progress-text');
                if (el) el.textContent = text;
            },
            close: () => {
                if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
            },
        };
    }


    function _showAutoHideToast(message, durationMs) {
        _injectStyles();
        const toast = document.createElement('div');
        toast.className = 'mp-gtu-toast';
        toast.setAttribute('role', 'status');
        toast.setAttribute('aria-live', 'polite');
        toast.textContent = message;
        document.body.appendChild(toast);

        const hideAfter = Number(durationMs) > 0 ? Number(durationMs) : 5000;
        global.setTimeout(() => {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, hideAfter);
    }

    // ─── Tx helpers ───────────────────────────────────────────────────────
    async function _waitForReceipt(provider, txHash, attempts) {
        for (let i = 0; i < attempts; i++) {
            try {
                const r = await provider.request({
                    method: 'eth_getTransactionReceipt',
                    params: [txHash],
                });
                if (r) return r;
            } catch (_) { /* retry */ }
            await new Promise((res) => setTimeout(res, 2000));
        }
        return null;
    }

    function _isUserRejected(err) {
        if (!err) return false;
        if (err.code === 4001) return true;
        const msg = String((err && (err.message || err.shortMessage)) || '').toLowerCase();
        return /reject|denied|user denied|user rejected/.test(msg);
    }

    function _sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function _jsonHeaders() {
        return { 'Content-Type': 'application/json' };
    }

    async function _postJson(url, payload) {
        const res = await fetch(url, {
            method: 'POST',
            headers: _jsonHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify(payload || {}),
        });
        let data = {};
        try { data = await res.json(); } catch (_) { data = {}; }
        if (!res.ok && !data.error && !data.reason) {
            data.error = 'Request failed with HTTP ' + res.status;
        }
        data.http_status = res.status;
        return data;
    }

    function _getCeloFaucetTerminal(result) {
        return result && (result.terminal_status || result.status || result.reason);
    }

    function _isCeloFaucetReady(result) {
        return !!(result && (result.gas_ready || result.status === 'gas_ready'));
    }

    function _isCeloFaucetHardStop(result) {
        const terminal = _getCeloFaucetTerminal(result);
        return terminal === 'gooddollar_cooldown'
            || terminal === 'recent_refill'
            || terminal === 'force_onchain_rate_limited'
            || terminal === 'not_configured';
    }

    function _showCeloFaucetCoverageBanner(result, walletAddr) {
        try {
            if (!global.GMGasCoverageBanner || !result) return;
            if (result.show_gas_coverage_message) {
                global.GMGasCoverageBanner.maybeShow(result, { wallet: walletAddr });
            } else if (_getCeloFaucetTerminal(result) === 'gooddollar_cooldown') {
                global.GMGasCoverageBanner.maybeShow(
                    Object.assign({}, result, { show_gas_coverage_message: true }),
                    { wallet: walletAddr }
                );
            }
        } catch (_) { /* banner is best-effort */ }
    }

    async function _pollCeloFaucetStatus(walletAddr, correlationId, maxDurationMs, progress) {
        const startedAt = Date.now();
        const schedule = [3000, 5000, 7000, 9000, 11000, 13000, 15000, 18000, 22000, 26000];
        let attempt = 0;
        let latest = null;

        while ((Date.now() - startedAt) < maxDurationMs) {
            const delay = schedule[Math.min(attempt, schedule.length - 1)];
            await _sleep(delay);
            attempt += 1;
            if (progress) progress.update('Step 1/3 — waiting for CELO faucet confirmation… (' + attempt + ')');
            latest = await _postJson('/api/faucet/status', {
                wallet: walletAddr,
                correlation_id: correlationId,
            });
            _showCeloFaucetCoverageBanner(latest, walletAddr);
            if (_isCeloFaucetReady(latest) || _isCeloFaucetHardStop(latest)) {
                return latest;
            }
        }
        return latest || {
            success: false,
            status: 'faucet_timeout',
            error: 'CELO faucet did not confirm before timeout.',
        };
    }

    async function _ensureCeloGasFaucet(walletAddr, balances, progress) {
        const belowLocalFloor = isBelowCeloFaucetFloor(balances);
        // MiniPay must mirror the working MetaMask/Trust path: ask the backend
        // whether CELO gas is ready, and if the MiniPay wallet is below the
        // 0.1 CELO floor (or backend dynamic gas says it is short), let
        // /api/faucet/gas call GoodDollar first and TOPWALLET_KEY as fallback.
        // If CELO is already sufficient the backend returns gas_ready and no
        // faucet entitlement is spent.
        const correlationId = 'minipay-celo-' + Date.now().toString(36)
            + '-' + Math.random().toString(36).slice(2, 8);

        try {
            if (progress) progress.update('Step 1/3 — checking CELO faucet status…');
            const statusBefore = await _postJson('/api/faucet/status', {
                wallet: walletAddr,
                correlation_id: correlationId,
            });
            _showCeloFaucetCoverageBanner(statusBefore, walletAddr);
            if (_isCeloFaucetReady(statusBefore) || _isCeloFaucetHardStop(statusBefore)) {
                return statusBefore;
            }

            // Mirror the working injected MetaMask/Trust Wallet path: call the
            // unified endpoint first, which itself tries GoodDollar's API and
            // falls back to the TOPWALLET_KEY on-chain topWallet() signer when
            // the API is down, rejected, or accepted but no balance arrives.
            if (progress) {
                progress.update(
                    belowLocalFloor
                        ? 'Step 1/3 — CELO is below ' + CELO_FAUCET_TRIGGER_BELOW_STR + '; requesting GoodDollar faucet with TOPWALLET fallback…'
                        : 'Step 1/3 — requesting CELO faucet with TOPWALLET fallback…'
                );
            }
            let result = await _postJson(CELO_FAUCET_ENDPOINT, {
                wallet: walletAddr,
                correlation_id: correlationId,
                force_onchain: false,
                client: 'minipay',
            });
            _showCeloFaucetCoverageBanner(result, walletAddr);

            if (_isCeloFaucetReady(result) || _isCeloFaucetHardStop(result)) {
                return result;
            }

            // If the unified request reports a transient failure before it ever
            // tried the on-chain signer, ask once for the explicit force_onchain
            // path. The backend rate-limits this path and still records the same
            // GoodDollar cooldown, so this matches injected-wallet behavior
            // without opening an unlimited TOPWALLET drain.
            const terminal = _getCeloFaucetTerminal(result);
            if (!result.gas_ready
                && result.attempted_onchain === false
                && (terminal === 'api_failed' || terminal === 'onchain_failed' || terminal === 'faucet_timeout')) {
                if (progress) progress.update('Step 1/3 — retrying CELO faucet through TOPWALLET fallback…');
                result = await _postJson(CELO_FAUCET_ENDPOINT, {
                    wallet: walletAddr,
                    correlation_id: correlationId,
                    force_onchain: true,
                    client: 'minipay',
                });
                _showCeloFaucetCoverageBanner(result, walletAddr);
                if (_isCeloFaucetReady(result) || _isCeloFaucetHardStop(result)) {
                    return result;
                }
            }

            // MetaMask/Trust Wallet do not trust a single immediate response;
            // they poll /api/faucet/status until the backend sees gas. MiniPay
            // needs the same wait because GoodDollar/API/topWallet credits can
            // confirm after the request returns or after the wallet RPC lags.
            const polled = await _pollCeloFaucetStatus(walletAddr, correlationId, 180000, progress);
            if (_isCeloFaucetReady(polled) || _isCeloFaucetHardStop(polled)) {
                return polled;
            }
            return Object.assign({}, result, { status_result: polled });
        } catch (err) {
            console.warn('[MPGasTopUp] CELO gas faucet request failed:', err);
            return { success: false, error: (err && err.message) || 'CELO faucet request failed' };
        }
    }

    async function _ensureCusdFaucet(walletAddr, progress) {
        if (progress) progress.update('Step 2/3 — sending ~' + CUSD_FAUCET_DISPLAY_AMOUNT + ' cUSD gas budget to your MiniPay wallet… ' + CUSD_FAUCET_PROGRAM_LABEL + '.');
        try {
            return await _postJson(CUSD_FAUCET_ENDPOINT, { wallet: walletAddr });
        } catch (err) {
            console.warn('[MPGasTopUp] cUSD faucet request failed:', err);
            return { success: false, error: (err && err.message) || 'cUSD faucet request failed' };
        }
    }

    async function _waitForStablecoin(walletAddr, attempts, progress) {
        const maxAttempts = attempts || 30;
        let latest = null;
        for (let i = 0; i < maxAttempts; i++) {
            latest = await getBalances(walletAddr);
            if (hasStablecoinGasBalance(latest)) return latest;
            if (progress) progress.update('Step 3/3 — waiting for cUSD to arrive… (' + (i + 1) + '/' + maxAttempts + ')');
            await _sleep(2000);
        }
        return latest || await getBalances(walletAddr);
    }


    function _describeCeloFaucetFailure(result) {
        if (!result) return 'GoodDollar CELO faucet did not return a result.';
        const terminal = result.terminal_status || result.status || result.reason;
        if (terminal === 'gooddollar_cooldown') {
            const secs = result.gooddollar_cooldown_remaining_seconds || result.recent_refill_cooldown_seconds;
            const human = _humanizeCooldownSeconds(secs) || 'a while';
            return result.reason || result.error || ('GoodDollar CELO faucet is on cooldown for ~' + human + '.');
        }
        if (terminal === 'recent_refill') {
            const human = _humanizeCooldownSeconds(result.recent_refill_cooldown_seconds) || 'a while';
            return result.reason || ('CELO faucet refill cooldown is active for ~' + human + '.');
        }
        if (terminal === 'api_accepted_pending') {
            return 'GoodDollar CELO faucet accepted the request, but CELO has not arrived yet. Please retry in a few seconds.';
        }
        return result.error || result.reason || 'GoodDollar CELO faucet did not send CELO to this MiniPay wallet.';
    }

    // ─── Swap execution: Uniswap V3 exactInputSingle CELO -> cUSD ─────────
    async function _swapCeloForCusd(walletAddr, amountCeloWei, progress) {
        const ethers = await _loadEthers();
        const provider = _getProvider();
        if (!provider) throw new Error('No injected wallet detected.');

        const iface = new ethers.Interface([
            'function exactInputSingle((address tokenIn,address tokenOut,uint24 fee,address recipient,uint256 amountIn,uint256 amountOutMinimum,uint160 sqrtPriceLimitX96) params) payable returns (uint256 amountOut)',
            'function approve(address spender,uint256 amount) returns (bool)',
            'function allowance(address owner,address spender) view returns (uint256)',
        ]);

        // Step 1: ensure SwapRouter has ERC20 allowance to pull our CELO.
        // (CELO is an ERC20 on Celo — no wrap needed.)
        const allowanceCalldata = iface.encodeFunctionData('allowance', [walletAddr, UNISWAP_ROUTER]);
        const allowanceHex = await _ethCall(provider, CELO, allowanceCalldata);
        const allowance = BigInt(allowanceHex || '0x0');

        if (allowance < amountCeloWei) {
            if (progress) progress.update('Step 1/2 — approve CELO in your wallet…');
            const approveCalldata = iface.encodeFunctionData('approve', [UNISWAP_ROUTER, amountCeloWei]);
            const approveTxHash = await provider.request({
                method: 'eth_sendTransaction',
                params: [{
                    from: walletAddr,
                    to: CELO,
                    data: approveCalldata,
                    value: '0x0',
                }],
            });
            if (progress) progress.update('Step 1/2 — waiting for approve to confirm…');
            const approveReceipt = await _waitForReceipt(provider, approveTxHash, 30);
            if (!approveReceipt) {
                throw new Error('Approve tx receipt timeout.');
            }
            if (approveReceipt.status === '0x0') {
                throw new Error('Approve reverted on-chain.');
            }
        }

        // Step 2: try exactInputSingle through known fee tiers, deepest first.
        let lastErr;
        for (const fee of FEE_TIERS) {
            const params = {
                tokenIn: CELO,
                tokenOut: CUSD,
                fee: fee,
                recipient: walletAddr,
                amountIn: amountCeloWei,
                // Slippage guard not strictly necessary for the dust amount
                // of cUSD involved here, but setting amountOutMinimum=0 with
                // sqrtPriceLimitX96=0 is the standard "best-effort" pattern
                // for tiny amounts.
                amountOutMinimum: 0n,
                sqrtPriceLimitX96: 0n,
            };
            const calldata = iface.encodeFunctionData('exactInputSingle', [params]);
            try {
                if (progress) progress.update('Step 2/2 — confirm swap (fee tier ' + (fee / 10000) + '%) in your wallet…');
                const swapTxHash = await provider.request({
                    method: 'eth_sendTransaction',
                    params: [{
                        from: walletAddr,
                        to: UNISWAP_ROUTER,
                        data: calldata,
                        value: '0x0',
                    }],
                });
                if (progress) progress.update('Swap submitted, waiting for confirmation…');
                const receipt = await _waitForReceipt(provider, swapTxHash, 60);
                if (!receipt) throw new Error('Swap tx receipt timeout.');
                if (receipt.status === '0x0') throw new Error('Swap reverted on-chain.');
                return swapTxHash;
            } catch (err) {
                lastErr = err;
                if (_isUserRejected(err)) throw err;
                console.warn('[MPGasTopUp] swap fee=' + fee + ' failed:', err && (err.message || err));
            }
        }
        throw lastErr || new Error('All Uniswap V3 fee tiers failed for CELO -> cUSD.');
    }

    // ─── Public: ensureToppedUp ───────────────────────────────────────────
    async function ensureToppedUp(walletAddr, opts) {
        opts = opts || {};
        if (!_isMiniPay()) {
            return { proceed: true, skipped: true, reason: 'not-minipay' };
        }
        if (!walletAddr) {
            return { proceed: true, skipped: true, reason: 'no-wallet' };
        }

        let balances;
        try {
            balances = await getBalances(walletAddr);
        } catch (err) {
            // Never silently continue to wallet approval on MiniPay when we
            // cannot read balances. Throw so callers can run their server-side
            // fallback (/api/faucet/status + /api/faucet/gas), which requests
            // GoodDollar faucet + GoodMarket fallback before approval.
            console.warn('[MPGasTopUp] balance probe failed; forcing fallback pre-flight:', err);
            throw new Error('MiniPay gas pre-check could not read wallet balances.');
        }

        const startedWithoutStableGas = !hasStablecoinGasBalance(balances);
        let faucetResult = null;
        let cooldownActive = false;
        let cooldownSeconds = 0;

        // Only run the gas top-up flow when the user actually lacks stablecoin
        // gas. If the wallet already holds ≥ STABLECOIN_GAS_MIN_USD of cUSD /
        // USDT / USDC, MiniPay can pay gas with it directly — there is no
        // reason to call the GoodDollar CELO faucet or the cUSD faucet, and
        // the autoswap prompt is unnecessary for those wallets.
        if (startedWithoutStableGas) {
            const progress = _showProgressModal(
                'Preparing MiniPay gas faucet…',
                '⛽ Preparing MiniPay stablecoin gas'
            );
            try {
                // Step 1/3: top up CELO so the wallet has gas to convert to
                // cUSD later. The CELO faucet is a no-op (gas_ready) if the
                // wallet already holds enough CELO.
                await _ensureCeloGasFaucet(walletAddr, balances, progress);
                try {
                    balances = await getBalances(walletAddr);
                } catch (_) { /* keep the pre-flight balances */ }

                // Step 2/3: ask goodmarket TOPWALLET_KEY to send the cUSD gas
                // budget so MiniPay's CIP-64 fee abstraction has stablecoin
                // to deduct from.
                faucetResult = await _ensureCusdFaucet(walletAddr, progress);
                cooldownActive = !!(faucetResult
                    && faucetResult.status === 'recent_refill');
                cooldownSeconds = (faucetResult
                    && Number(faucetResult.recent_refill_cooldown_seconds)) || 0;

                // Step 3/3: regardless of whether the cUSD faucet endpoint
                // itself reported success, poll the on-chain balance. As long
                // as the wallet ends up with ≥ STABLECOIN_GAS_MIN_USD of
                // stablecoin (from goodmarket's faucet, an external transfer,
                // or any other source), MiniPay can pay the swap gas — fall
                // through to the autoswap prompt. The poll window is short
                // when the server told us a refill cooldown is active (no
                // cUSD will arrive from goodmarket then).
                const pollAttempts = cooldownActive ? 5 : 30;
                balances = await _waitForStablecoin(walletAddr, pollAttempts, progress);
                progress.close();

                if (!hasStablecoinGasBalance(balances)) {
                    let msg;
                    if (cooldownActive) {
                        const human = _humanizeCooldownSeconds(cooldownSeconds) || 'a few hours';
                        const readyAt = _formatCooldownReadyAt(cooldownSeconds);
                        const tail = readyAt ? ' (until ' + readyAt + ')' : '';
                        msg = '⏳ MiniPay cUSD faucet is on cooldown\n\n'
                            + 'You received the gas budget recently. Wait ~' + human + tail
                            + ' or send a small amount of cUSD / USDT / USDC to your '
                            + 'MiniPay wallet from another source. MiniPay needs stablecoin gas before it can swap CELO to cUSD.';
                        if (typeof global.alert === 'function') global.alert(msg);
                        return {
                            proceed: false,
                            cooldown: true,
                            cooldownSeconds: cooldownSeconds,
                            faucetResult: faucetResult,
                        };
                    }
                    if (faucetResult && !faucetResult.success
                        && faucetResult.status !== 'stable_ready') {
                        msg = 'MiniPay cUSD faucet failed: '
                            + (faucetResult.error || faucetResult.reason || 'unknown error.');
                    } else {
                        msg = 'cUSD faucet was requested, but stablecoin has not arrived yet. Please retry in a few seconds.';
                    }
                    if (typeof global.alert === 'function') global.alert(msg);
                    return { proceed: false, error: msg, faucetResult: faucetResult };
                }

                if (faucetResult && faucetResult.status === 'cusd_sent') {
                    _showAutoHideToast(
                        "✅ GoodMarket gas received. Don\'t transfer this cUSD to another wallet to avoid next-claim errors.",
                        5000
                    );
                }
                // Wallet has stablecoin gas now. Some callers (notably the
                // user's own CELO -> cUSD swap) only need the GoodMarket cUSD
                // faucet to unlock MiniPay gas; doing a separate auto-swap here
                // would duplicate the action they already requested.
                if (opts.stableGasOnly || opts.skipAutoSwap) {
                    return {
                        proceed: true,
                        stableGasOnly: true,
                        swapped: false,
                        faucetResult: faucetResult,
                    };
                }

                // Otherwise fall through to the autoswap prompt below.
            } catch (err) {
                progress.close();
                const msg = (err && err.message) || 'MiniPay stablecoin faucet failed.';
                console.warn('[MPGasTopUp] stablecoin faucet pre-flight failed:', err);
                return { proceed: false, error: msg, faucetResult: faucetResult };
            }
        }

        const shouldPromptSwap = startedWithoutStableGas || needsTopUpFromBalances(balances);
        const amountWei = getAutoSwapAmountWei(balances);
        
        // CRITICAL: Final gas readiness check before proceeding.
        // If user has no stablecoin gas AND no CELO to swap, they CANNOT pay
        // for any transaction. Block proceeding to wallet approval.
        if (!hasStablecoinGasBalance(balances) && amountWei <= 0n) {
            const msg = '⚠️ Insufficient gas for MiniPay\n\n'
                + 'Your wallet has less than 0.01 cUSD (stablecoin gas) AND less than 0.09 CELO (nothing to swap).\n\n'
                + 'MiniPay pays transaction fees in stablecoins (cUSD/USDT/USDC). '
                + 'Please add some cUSD, USDT, or USDC to your wallet, or wait for the gas faucet cooldown to expire and retry.';
            if (typeof global.alert === 'function') global.alert(msg);
            return {
                proceed: false,
                error: 'Insufficient gas: no stablecoin and no CELO to swap.',
                insufficientGas: true,
                balances: {
                    hasStablecoin: false,
                    hasCeloToSwap: false,
                    stablecoinUsd: _stablecoinUsdTotal(balances),
                    celoWei: balances.celo ? balances.celo.toString() : '0',
                },
            };
        }
        
        if (!shouldPromptSwap || amountWei <= 0n) {
            return {
                proceed: true,
                skipped: true,
                reason: hasStablecoinGasBalance(balances) ? 'stablecoin-gas-ready' : 'no-celo-to-swap',
                stableFaucet: faucetResult,
            };
        }

        let _modalBody;
        if (opts.body) {
            _modalBody = opts.body;
        } else if (cooldownActive) {
            const human = _humanizeCooldownSeconds(cooldownSeconds) || 'some time';
            _modalBody = 'The cUSD gas faucet is on cooldown for another ~' + human + '. '
                + 'You have CELO — convert the amount above the 0.09 CELO reserve to cUSD '
                + 'so you can pay gas and continue. '
                + '<br><br>⚠️ <strong>Do not transfer the resulting cUSD to another wallet</strong> — '
                + 'it\'s needed as gas for your next claims.';
        } else if (startedWithoutStableGas) {
            _modalBody = '✅ We sent a small cUSD gas budget to your MiniPay wallet — '
                + '<em>Program by Betz Team.</em>'
                + '<br><br>'
                + 'Next, convert your CELO to cUSD. <strong>MiniPay does not use CELO for gas</strong> — '
                + 'stablecoin (cUSD/USDT/USDC) is needed for your next claims. '
                + 'You\'ll sign the swap inside MiniPay; we\'ll keep ~0.09 CELO as your MiniPay reserve. '
                + '<br><br>⚠️ <strong>Do not transfer the cUSD to another wallet.</strong>';
        } else {
            _modalBody = 'You\'re doing an action that needs gas. MiniPay pays gas in '
                + '<strong>stablecoin (cUSD/USDT/USDC), not in CELO</strong> — '
                + 'you need to convert your CELO to cUSD first to pay for gas.'
                + '<br><br>'
                + 'You\'ll sign the swap inside MiniPay. We\'ll keep ~0.09 CELO untouched as your MiniPay reserve. '
                + '⚠️ <strong>Do not transfer the resulting cUSD to another wallet</strong> — '
                + 'it\'s needed as gas for your next claims.';
        }

        const confirmed = await _showConfirmModal({
            body: _modalBody,
            amountCelo: _formatCelo(amountWei),
            reserveCelo: CELO_RESERVE_AFTER_TOPUP_STR,
            celoFmt: _formatCelo(balances.celo),
        });
        if (!confirmed) return { proceed: false, cancelled: true };

        const progress = _showProgressModal('Preparing swap…');
        try {
            const txHash = await _swapCeloForCusd(walletAddr, amountWei, progress);
            progress.update('Swap confirmed ✓ — waiting a moment for balances to settle…');
            await new Promise((r) => setTimeout(r, 3000));
            progress.close();
            return { proceed: true, swapped: true, txHash: txHash };
        } catch (err) {
            progress.close();
            const msg = (err && (err.message || err.shortMessage)) || 'Swap failed.';
            if (_isUserRejected(err)) {
                return { proceed: false, cancelled: true };
            }
            console.error('[MPGasTopUp] swap error:', err);
            try {
                if (typeof global.alert === 'function') {
                    global.alert(
                        'Gas top-up swap failed: ' + msg + '\n\n'
                        + 'You can still retry the original action — if the network '
                        + 'has any way to pay gas it will go through, otherwise it '
                        + 'will surface a wallet-side error.'
                    );
                }
            } catch (_) { /* ignore */ }
            return { proceed: false, error: msg };
        }
    }

    // ─── Public: runWithGasTopUp ──────────────────────────────────────────
    async function runWithGasTopUp(walletAddr, action, opts) {
        const result = await ensureToppedUp(walletAddr, opts);
        if (!result.proceed) {
            return Object.assign({ ranAction: false }, result);
        }
        if (typeof action !== 'function') {
            return Object.assign({ ranAction: false }, result, { reason: 'no-action' });
        }
        const actionResult = await action();
        return {
            ranAction: true,
            swapped: !!result.swapped,
            actionResult: actionResult,
        };
    }

    // ─── Public: passive banner ───────────────────────────────────────────
    async function maybeShowBanner(walletAddr, opts) {
        opts = opts || {};
        if (!_isMiniPay()) return false;
        if (!walletAddr) return false;
        let balances;
        try {
            balances = await getBalances(walletAddr);
        } catch (_) { return false; }
        if (!needsTopUpFromBalances(balances)) return false;

        _injectStyles();
        const containerId = opts.containerId || null;
        const container = containerId
            ? document.getElementById(containerId)
            : document.body;
        if (!container) return false;
        if (document.getElementById('mp-gtu-banner')) return false;

        const banner = document.createElement('div');
        banner.id = 'mp-gtu-banner';
        banner.className = 'mp-gtu-banner';
        banner.innerHTML = ''
            + '<div class="mp-gtu-banner-text">'
            + '<strong>⛽ Heads up:</strong> You have CELO but no stablecoin. '
            + 'Most actions in MiniPay pay gas in stablecoin (cUSD / USDT / USDC).'
            + '</div>'
            + '<button class="mp-gtu-banner-btn" id="mp-gtu-banner-btn">'
            + 'Convert available CELO → cUSD'
            + '</button>';

        if (opts.insertBefore && container.contains(opts.insertBefore)) {
            container.insertBefore(banner, opts.insertBefore);
        } else {
            container.prepend(banner);
        }
        document.getElementById('mp-gtu-banner-btn').addEventListener('click', async () => {
            const r = await ensureToppedUp(walletAddr);
            if (r.proceed && r.swapped) {
                banner.style.display = 'none';
            }
        });
        return true;
    }

    global.MPGasTopUp = {
        isMiniPay: _isMiniPay,
        setMiniPayDetected: _setMiniPayDetected,
        getBalances: getBalances,
        needsTopUpFromBalances: needsTopUpFromBalances,
        hasStablecoinGasBalance: hasStablecoinGasBalance,
        isBelowCeloFaucetFloor: isBelowCeloFaucetFloor,
        ensureToppedUp: ensureToppedUp,
        runWithGasTopUp: runWithGasTopUp,
        maybeShowBanner: maybeShowBanner,
        constants: {
            CELO: CELO, CUSD: CUSD, USDT: USDT, USDC: USDC,
            UNISWAP_ROUTER: UNISWAP_ROUTER,
            CELO_RESERVE_AFTER_TOPUP_STR: CELO_RESERVE_AFTER_TOPUP_STR,
            CELO_FAUCET_TRIGGER_BELOW_STR: CELO_FAUCET_TRIGGER_BELOW_STR,
            STABLECOIN_GAS_MIN_USD: STABLECOIN_GAS_MIN_USD,
            CUSD_FAUCET_PROGRAM_LABEL: CUSD_FAUCET_PROGRAM_LABEL,
        },
    };
})(typeof window !== 'undefined' ? window : globalThis);
