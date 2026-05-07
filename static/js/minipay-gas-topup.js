/*
 * minipay-gas-topup.js
 *
 * Auto-detect + auto-prompt UX for MiniPay users who hold CELO but no
 * stablecoin balance (cUSD / USDT / USDC). MiniPay pays gas only in
 * stablecoins, so a CELO-only user gets stuck on every on-chain action
 * (claim, swap, send, bridge, etc.).
 *
 * Flow:
 *   1) Detect: isMiniPay() + CELO > threshold + zero stablecoins
 *   2) Confirm modal: "Convert ~0.1 CELO -> cUSD now? (~$0.05)"
 *   3) Run Uniswap V3 exactInputSingle CELO -> cUSD with feeCurrency
 *      defaulted to CELO native (the swap pays its own gas).
 *   4) Resume the original action via runWithGasTopUp(wallet, action).
 *
 * Constraint: every on-chain tx still requires user signature inside
 * MiniPay, so this is "auto-prompt + chained txs", not "zero-tap auto".
 *
 * Public API (window.MPGasTopUp):
 *   - isMiniPay()                            -> bool
 *   - getBalances(walletAddr)                -> { celo, cusd, usdt, usdc } as bigint
 *   - needsTopUpFromBalances(balances)       -> bool
 *   - ensureToppedUp(walletAddr, opts)       -> Promise<{ proceed, swapped?, cancelled?, error? }>
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

    // Default top-up amount in CELO. Sized to cover dozens of MiniPay txs
    // even at high gas prices while staying low-cost (~$0.05 at $0.6/CELO).
    const TOPUP_AMOUNT_CELO_STR = '0.1';

    // MiniPay needs a small stablecoin balance to pay fee-currency gas.
    const STABLECOIN_GAS_MIN_USD = 0.05;
    const CELO_GAS_FAUCET_MIN_CELO = 0.1;
    const CUSD_FAUCET_ENDPOINT = '/api/minipay/stablecoin-faucet';
    const CELO_FAUCET_ENDPOINT = '/api/faucet/gas';

    // Minimum CELO balance required to attempt a top-up. Needs enough for
    // both the swap output and the swap's own gas + approve gas.
    const MIN_CELO_TO_TOPUP = 0.15;

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
    function _isMiniPay() {
        try {
            const eth = global.ethereum;
            if (eth && eth.isMiniPay) return true;
            if (eth && Array.isArray(eth.providers)
                && eth.providers.some(p => p && p.isMiniPay)) return true;
            if (typeof navigator !== 'undefined'
                && /minipay/i.test(navigator.userAgent || '')) return true;
        } catch (_) { /* swallow */ }
        return false;
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

    function needsTopUpFromBalances(balances) {
        const celoMinWei = BigInt(Math.floor(MIN_CELO_TO_TOPUP * 1e18));
        return balances.celo > celoMinWei && !hasStablecoinGasBalance(balances);
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
            + '.mp-gtu-banner{margin:0 auto 1rem;max-width:600px;'
            + 'background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.4);'
            + 'border-radius:12px;padding:.8rem 1rem;color:#fbbf24;'
            + 'font-size:.85rem;display:flex;gap:.7rem;align-items:center;'
            + 'flex-wrap:wrap;}'
            + '.mp-gtu-banner-text{flex:1;min-width:200px;line-height:1.4;}'
            + '.mp-gtu-banner-btn{background:linear-gradient(135deg,#7c3aed,#6d28d9);'
            + 'color:#fff;border:none;border-radius:8px;padding:.45rem .9rem;'
            + 'cursor:pointer;font-size:.82rem;font-weight:600;white-space:nowrap;}';
        document.head.appendChild(style);
    }

    function _formatCelo(weiBig) {
        try {
            const num = Number(weiBig) / 1e18;
            return num.toFixed(4);
        } catch (_) { return '?'; }
    }

    function _showConfirmModal(opts) {
        _injectStyles();
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'mp-gtu-overlay';
            overlay.innerHTML = ''
                + '<div class="mp-gtu-card" role="dialog" aria-modal="true">'
                + '<div class="mp-gtu-title">⛽ Stablecoin gas needed</div>'
                + '<div class="mp-gtu-body">' + (opts.body || '') + '</div>'
                + '<div class="mp-gtu-status">'
                + 'Convert <strong>~' + opts.amountCelo + ' CELO</strong> → <strong>cUSD</strong> now? '
                + 'Current CELO balance: ' + opts.celoFmt + '. The swap itself uses a tiny bit of CELO for gas.'
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

    async function _ensureCeloGasFaucet(walletAddr, balances, progress) {
        const nativeMinWei = BigInt(Math.floor(CELO_GAS_FAUCET_MIN_CELO * 1e18));
        if (!balances || balances.celo >= nativeMinWei) {
            return { skipped: true, reason: 'celo-gas-ready' };
        }
        if (progress) progress.update('Step 1/3 — requesting CELO faucet because balance is below 0.1 CELO…');
        try {
            return await _postJson(CELO_FAUCET_ENDPOINT, { wallet: walletAddr });
        } catch (err) {
            console.warn('[MPGasTopUp] CELO gas faucet request failed:', err);
            return { success: false, error: (err && err.message) || 'CELO faucet request failed' };
        }
    }

    async function _ensureCusdFaucet(walletAddr, progress) {
        if (progress) progress.update('Step 2/3 — sending ~$0.05 cUSD gas budget to your MiniPay wallet…');
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
                // Slippage guard not strictly necessary for $0.05 of cUSD, but
                // setting amountOutMinimum=0 with sqrtPriceLimitX96=0 is the
                // standard "best-effort" pattern for tiny amounts.
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
            console.warn('[MPGasTopUp] balance probe failed; skipping pre-flight:', err);
            return { proceed: true, skipped: true, reason: 'balance-probe-failed' };
        }

        const startedWithoutStableGas = !hasStablecoinGasBalance(balances);
        let faucetResult = null;

        if (startedWithoutStableGas) {
            const progress = _showProgressModal(
                'Preparing MiniPay gas faucet…',
                '⛽ Preparing MiniPay stablecoin gas'
            );
            try {
                await _ensureCeloGasFaucet(walletAddr, balances, progress);
                faucetResult = await _ensureCusdFaucet(walletAddr, progress);
                if (!faucetResult.success
                    && faucetResult.status !== 'stable_ready'
                    && faucetResult.status !== 'recent_refill') {
                    const msg = faucetResult.error || faucetResult.reason || 'MiniPay cUSD faucet failed.';
                    progress.close();
                    if (typeof global.alert === 'function') {
                        global.alert('MiniPay cUSD faucet failed: ' + msg);
                    }
                    return { proceed: false, error: msg, faucetResult: faucetResult };
                }
                balances = await _waitForStablecoin(walletAddr, 30, progress);
                progress.close();
                if (!hasStablecoinGasBalance(balances)) {
                    const msg = 'cUSD faucet was requested, but stablecoin has not arrived yet. Please retry in a few seconds.';
                    if (typeof global.alert === 'function') global.alert(msg);
                    return { proceed: false, error: msg, faucetResult: faucetResult };
                }
            } catch (err) {
                progress.close();
                const msg = (err && err.message) || 'MiniPay stablecoin faucet failed.';
                console.warn('[MPGasTopUp] stablecoin faucet pre-flight failed:', err);
                return { proceed: false, error: msg, faucetResult: faucetResult };
            }
        }

        const shouldPromptSwap = startedWithoutStableGas || needsTopUpFromBalances(balances);
        const celoMinWei = BigInt(Math.floor(MIN_CELO_TO_TOPUP * 1e18));
        if (!shouldPromptSwap || balances.celo <= celoMinWei) {
            return {
                proceed: true,
                skipped: true,
                reason: hasStablecoinGasBalance(balances) ? 'stablecoin-gas-ready' : 'no-celo-to-swap',
                stableFaucet: faucetResult,
            };
        }

        const confirmed = await _showConfirmModal({
            body: opts.body
                || (startedWithoutStableGas
                    ? 'We sent a small cUSD gas budget to your MiniPay wallet. Now convert some CELO to cUSD so future MiniPay transactions can keep paying gas in stablecoin.'
                    : 'You\'re doing an action that needs gas. MiniPay pays gas in stablecoin (cUSD/USDT/USDC), but you only have CELO right now. Convert a small amount to cUSD first?'),
            amountCelo: TOPUP_AMOUNT_CELO_STR,
            celoFmt: _formatCelo(balances.celo),
        });
        if (!confirmed) return { proceed: false, cancelled: true };

        const progress = _showProgressModal('Preparing swap…');
        try {
            const amountWei = BigInt(Math.floor(parseFloat(TOPUP_AMOUNT_CELO_STR) * 1e18));
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
            + 'Convert ~' + TOPUP_AMOUNT_CELO_STR + ' CELO → cUSD'
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
        getBalances: getBalances,
        needsTopUpFromBalances: needsTopUpFromBalances,
        hasStablecoinGasBalance: hasStablecoinGasBalance,
        ensureToppedUp: ensureToppedUp,
        runWithGasTopUp: runWithGasTopUp,
        maybeShowBanner: maybeShowBanner,
        constants: {
            CELO: CELO, CUSD: CUSD, USDT: USDT, USDC: USDC,
            UNISWAP_ROUTER: UNISWAP_ROUTER,
            TOPUP_AMOUNT_CELO_STR: TOPUP_AMOUNT_CELO_STR,
            STABLECOIN_GAS_MIN_USD: STABLECOIN_GAS_MIN_USD,
        },
    };
})(typeof window !== 'undefined' ? window : globalThis);
