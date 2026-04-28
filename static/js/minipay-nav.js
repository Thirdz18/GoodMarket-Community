// MiniPay nav helper: hides Savings/Uniswap entry points for users in the
// MiniPay dapp browser. GoodReserve remains accessible. Detection is best-effort
// (provider flag + UA), and is mirrored server-side in select Flask routes.
(function () {
    function isMiniPay() {
        try {
            if (typeof window === 'undefined') return false;
            var eth = window.ethereum;
            if (eth && eth.isMiniPay) return true;
            if (eth && Array.isArray(eth.providers)
                && eth.providers.some(function (p) { return p && p.isMiniPay; })) return true;
            if (typeof navigator !== 'undefined'
                && /minipay/i.test(navigator.userAgent || '')) return true;
        } catch (_) { /* no-op */ }
        return false;
    }

    function hideSavingsLinks() {
        var sel = 'a[href="/savings"], a[href^="/savings?"], a[href^="/savings#"]';
        document.querySelectorAll(sel).forEach(function (el) {
            el.style.display = 'none';
        });
    }

    // Swap page hardening for MiniPay: never expose DEX/Uniswap UI if the
    // backend user-agent check misses MiniPay on first render.
    function applySwapMiniPayMode() {
        var tabSwitcher = document.getElementById('swapTabSwitcher');
        if (tabSwitcher) tabSwitcher.style.display = 'none';

        var dexBtn = document.getElementById('tabBtnDex');
        if (dexBtn) dexBtn.style.display = 'none';

        var dexPane = document.getElementById('swapPaneDex') || document.querySelector('.pane-dex');
        if (dexPane) dexPane.style.display = 'none';

        var reservePane = document.getElementById('swapPaneReserve') || document.querySelector('.pane-reserve');
        if (reservePane) {
            reservePane.style.display = '';
            reservePane.classList.remove('hidden-tab');
        }

        if (typeof window.setSwapTab === 'function') {
            try { window.setSwapTab('reserve'); } catch (_) { /* no-op */ }
        }
    }

    function apply() {
        if (!isMiniPay()) return;
        hideSavingsLinks();
        applySwapMiniPayMode();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', apply, { once: true });
    } else {
        apply();
    }
})();
