/*
 * minipay-fee-currency.js
 *
 * Shared Celo/MiniPay fee-currency helper for injected MiniPay and Privy
 * Connect Wallet sessions that are backed by MiniPay. It reads all supported
 * stablecoin balances through the active provider and orders feeCurrency
 * attempts by the balance the user actually has, then appends the remaining
 * stablecoin adapters and optional native fallback.
 */
(function (global) {
    'use strict';
    if (global.GMMinipayFeeCurrencies) return;

    const FEE_CURRENCY = {
        CUSD: '0x765DE816845861e75A25fCA122bb6898B8B1282a',
        USDT_ADAPTER: '0x0E2A3e05bc9A16F5292A6170456A710cb89C6f72',
        USDC_ADAPTER: '0x2F25deB3848C207fc8E0c34035B3Ba7fC157602B',
    };
    const TOKENS = [
        { key: 'cusd', token: FEE_CURRENCY.CUSD, feeCurrency: FEE_CURRENCY.CUSD, decimals: 18 },
        { key: 'usdt', token: '0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e', feeCurrency: FEE_CURRENCY.USDT_ADAPTER, decimals: 6 },
        { key: 'usdc', token: '0xcebA9300f2b948710d2653dD7B07f33A8B32118C', feeCurrency: FEE_CURRENCY.USDC_ADAPTER, decimals: 6 },
    ];

    function _balanceOfData(wallet) {
        return '0x70a08231' + String(wallet || '').toLowerCase().replace(/^0x/, '').padStart(64, '0');
    }

    async function _readBalance(provider, token, wallet) {
        try {
            if (!provider || !wallet || !/^0x[0-9a-fA-F]{40}$/.test(wallet)) return 0;
            const res = await provider.request({
                method: 'eth_call',
                params: [{ to: token, data: _balanceOfData(wallet) }, 'latest'],
            });
            return Number(BigInt(res || '0x0'));
        } catch (_) {
            return 0;
        }
    }

    async function getBalances(provider, wallet) {
        const rows = await Promise.all(TOKENS.map(async (t) => {
            const raw = await _readBalance(provider, t.token, wallet);
            return Object.assign({}, t, { raw: raw, value: raw / Math.pow(10, t.decimals) });
        }));
        return rows.reduce((acc, row) => {
            acc[row.key] = row.value;
            return acc;
        }, {});
    }

    async function orderByBalances(provider, wallet, options) {
        const opts = options || {};
        const rows = await Promise.all(TOKENS.map(async (t) => {
            const explicit = opts[t.key + 'Balance'];
            const raw = explicit === undefined ? await _readBalance(provider, t.token, wallet) : Number(explicit) * Math.pow(10, t.decimals);
            return Object.assign({}, t, { value: raw / Math.pow(10, t.decimals) });
        }));
        const seen = new Set();
        const ordered = [];
        rows
            .filter((row) => row.value > 0 && row.feeCurrency)
            .sort((a, b) => b.value - a.value)
            .forEach((row) => {
                if (!seen.has(row.feeCurrency)) {
                    seen.add(row.feeCurrency);
                    ordered.push(row.feeCurrency);
                }
            });
        rows.forEach((row) => {
            if (row.feeCurrency && !seen.has(row.feeCurrency)) {
                seen.add(row.feeCurrency);
                ordered.push(row.feeCurrency);
            }
        });
        if (opts.includePlain !== false) ordered.push(null);
        return ordered;
    }

    global.GMMinipayFeeCurrencies = { FEE_CURRENCY, getBalances, orderByBalances };
})(typeof window !== 'undefined' ? window : this);
