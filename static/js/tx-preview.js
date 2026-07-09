/**
 * GoodMarket transaction-preview module.
 *
 * Wraps every wallet-bound transaction with a plain-English confirmation
 * step BEFORE the wallet popup is opened. The user reads what they're about
 * to do in their own language, and only after they accept does the actual
 * `eth_sendTransaction` / contract call go through.
 *
 * This is defense in depth against the worst-case scenario for a
 * non-custodial DeFi app: an attacker compromises the frontend (CDN, hosting,
 * dependency) and swaps the contract address or amount. With this preview,
 * the user sees:
 *
 *      You are about to APPROVE
 *      The Uniswap router (0x1234…5678)
 *      to spend up to: 100 G$ from your wallet
 *      Network: Celo
 *
 * — instead of a hex-encoded calldata blob the user can't read. If the
 * preview shows something unexpected (wrong contract, wrong amount, wrong
 * network), the user can cancel before signing.
 *
 * USAGE:
 *
 *   await GMTxPreview.confirm({
 *     action:    'approve',           // 'approve' | 'transfer' | 'swap' | 'send' | 'sign'
 *     token:     'G$',                // human-readable token name
 *     to:        '0xRouter…',         // contract / recipient
 *     toLabel:   'Uniswap V2 Router', // friendly label for `to`
 *     amount:    '100',               // human amount (string), or 'unlimited'
 *     network:   'Celo',
 *     note:      'You will be able to swap up to 100 G$ in this session.',
 *   });
 *   // Throws GMTxPreview.Cancelled if the user clicks "Cancel" — caller
 *   // should catch it and abort the flow without showing an error toast.
 *   await wallet.sendTransaction(...);
 *
 * The module is intentionally framework-free (no React/Vue) so it can drop
 * into every existing template. Lazy-injects its own DOM + styles on first
 * use; idempotent.
 */

(function (root) {
    'use strict';

    const STATE = { mounted: false, root: null };

    class Cancelled extends Error {
        constructor() {
            super('User cancelled the transaction preview.');
            this.name = 'GMTxPreviewCancelled';
        }
    }

    function escapeHtml(s) {
        if (s === null || s === undefined) return '';
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function shortAddr(addr) {
        if (!addr || typeof addr !== 'string') return '';
        if (addr.length <= 12) return addr;
        return addr.slice(0, 6) + '…' + addr.slice(-4);
    }

    function ensureMounted() {
        if (STATE.mounted) return;
        const css = `
            .gm-txp-overlay {
                position: fixed; inset: 0; z-index: 2147483600;
                background: rgba(15, 23, 42, .78);
                display: none; align-items: center; justify-content: center;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
            }
            .gm-txp-overlay.show { display: flex; }
            .gm-txp-card {
                background: #ffffff; color: #0f172a;
                border-radius: 16px;
                width: min(440px, calc(100vw - 32px));
                max-height: calc(100vh - 32px); overflow-y: auto;
                padding: 24px;
                box-shadow: 0 20px 60px rgba(0,0,0,.35);
                border: 1px solid rgba(99,102,241,.2);
            }
            @media (prefers-color-scheme: dark) {
                .gm-txp-card { background: #0f172a; color: #f1f5f9; border-color: rgba(99,102,241,.35); }
                .gm-txp-row { border-color: rgba(99,102,241,.18) !important; }
            }
            .gm-txp-title {
                font-size: 18px; font-weight: 700; margin: 0 0 4px;
                display: flex; align-items: center; gap: 8px;
            }
            .gm-txp-title .gm-txp-badge {
                font-size: 11px; font-weight: 700; text-transform: uppercase;
                background: #6366f1; color: white; padding: 3px 8px; border-radius: 999px;
                letter-spacing: .5px;
            }
            .gm-txp-title .gm-txp-badge.danger { background: #dc2626; }
            .gm-txp-title .gm-txp-badge.warn { background: #f59e0b; }
            .gm-txp-subtitle { font-size: 13px; opacity: .7; margin: 0 0 16px; }
            .gm-txp-row {
                display: flex; justify-content: space-between; gap: 12px;
                padding: 10px 0;
                border-bottom: 1px solid rgba(15,23,42,.08);
                font-size: 14px;
            }
            .gm-txp-row:last-of-type { border-bottom: none; }
            .gm-txp-key { opacity: .65; flex-shrink: 0; }
            .gm-txp-val {
                font-weight: 600; text-align: right; word-break: break-all;
                font-feature-settings: 'tnum';
            }
            .gm-txp-val.danger { color: #dc2626; }
            .gm-txp-val.mono {
                font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                font-size: 12px;
            }
            .gm-txp-warn {
                margin-top: 12px; padding: 10px 12px;
                background: rgba(245, 158, 11, .12); color: #78350f;
                border-left: 3px solid #f59e0b; border-radius: 6px;
                font-size: 12.5px; line-height: 1.45;
            }
            @media (prefers-color-scheme: dark) {
                .gm-txp-warn { color: #fcd34d; background: rgba(245,158,11,.10); }
            }
            .gm-txp-actions {
                display: flex; gap: 8px; margin-top: 18px;
            }
            .gm-txp-btn {
                flex: 1; border: none; padding: 12px 14px; border-radius: 10px;
                font-size: 15px; font-weight: 600; cursor: pointer;
                transition: transform .06s ease, opacity .15s ease;
            }
            .gm-txp-btn:active { transform: scale(.98); }
            .gm-txp-btn.cancel {
                background: rgba(15,23,42,.07); color: #0f172a;
            }
            @media (prefers-color-scheme: dark) {
                .gm-txp-btn.cancel { background: rgba(241,245,249,.10); color: #f1f5f9; }
            }
            .gm-txp-btn.confirm {
                background: linear-gradient(135deg, #6366f1, #4f46e5);
                color: white;
            }
            .gm-txp-note {
                margin-top: 8px; font-size: 12px; opacity: .65; line-height: 1.45;
            }
        `;

        const style = document.createElement('style');
        style.id = 'gm-txp-style';
        style.textContent = css;
        document.head.appendChild(style);

        const overlay = document.createElement('div');
        overlay.className = 'gm-txp-overlay';
        overlay.id = 'gm-txp-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.innerHTML = `
            <div class="gm-txp-card" id="gm-txp-card"></div>
        `;
        document.body.appendChild(overlay);
        STATE.root = overlay;
        STATE.mounted = true;
    }

    function renderCard(opts) {
        const action = (opts.action || 'transaction').toLowerCase();
        const isApprove = action === 'approve';
        const isUnlimited = opts.amount === 'unlimited' ||
                            opts.amount === Infinity ||
                            (typeof opts.amount === 'string' && /unlimited|infinite|max/i.test(opts.amount));

        const badgeClass = isUnlimited ? 'danger' : (isApprove ? 'warn' : '');
        const titleByAction = {
            approve:  'Approve Spending',
            transfer: 'Send Tokens',
            swap:     'Swap Tokens',
            send:     'Confirm Transaction',
            sign:     'Sign Message',
        };
        const title = titleByAction[action] || 'Confirm Transaction';

        const rows = [];
        if (opts.token) {
            rows.push(['Token', escapeHtml(opts.token), '']);
        }
        if (opts.amount !== undefined && opts.amount !== null) {
            const amtClass = isUnlimited ? 'danger' : '';
            const amtTxt = isUnlimited ? '⚠️ UNLIMITED' : escapeHtml(String(opts.amount));
            rows.push(['Amount', amtTxt, amtClass]);
        }
        if (opts.to) {
            const toLabel = opts.toLabel
                ? `${escapeHtml(opts.toLabel)}<br><span class="gm-txp-val mono" style="opacity:.7;font-weight:500">${escapeHtml(shortAddr(opts.to))}</span>`
                : `<span class="mono">${escapeHtml(shortAddr(opts.to))}</span>`;
            rows.push([isApprove ? 'Spender' : 'Recipient', toLabel, '']);
        }
        if (opts.network) {
            rows.push(['Network', escapeHtml(opts.network), '']);
        }
        if (opts.fee) {
            rows.push(['Estimated fee', escapeHtml(opts.fee), '']);
        }

        const rowsHtml = rows.map(([k, v, cls]) => `
            <div class="gm-txp-row">
                <span class="gm-txp-key">${escapeHtml(k)}</span>
                <span class="gm-txp-val ${cls}">${v}</span>
            </div>
        `).join('');

        const warnings = [];
        if (isUnlimited) {
            warnings.push(
                '<strong>UNLIMITED approval.</strong> The contract above can spend ANY amount of this token from your wallet, at any time, until you revoke it. Only confirm if you fully trust this contract.'
            );
        }
        if (opts.warning) {
            warnings.push(escapeHtml(opts.warning));
        }
        const warnHtml = warnings.length
            ? `<div class="gm-txp-warn">${warnings.join('<br>')}</div>`
            : '';

        const noteHtml = opts.note
            ? `<div class="gm-txp-note">${escapeHtml(opts.note)}</div>`
            : '';

        return `
            <div class="gm-txp-title">
                ${escapeHtml(title)}
                <span class="gm-txp-badge ${badgeClass}">${escapeHtml(action)}</span>
            </div>
            <p class="gm-txp-subtitle">Read carefully. Your wallet will ask you to sign next.</p>
            ${rowsHtml}
            ${warnHtml}
            ${noteHtml}
            <div class="gm-txp-actions">
                <button class="gm-txp-btn cancel" id="gm-txp-cancel" type="button">Cancel</button>
                <button class="gm-txp-btn confirm" id="gm-txp-confirm" type="button">Confirm &amp; Sign</button>
            </div>
        `;
    }

    function confirm(opts) {
        opts = opts || {};
        ensureMounted();
        const overlay = STATE.root;
        const card = overlay.querySelector('#gm-txp-card');
        card.innerHTML = renderCard(opts);
        overlay.classList.add('show');
        document.body.style.overflow = 'hidden';

        return new Promise((resolve, reject) => {
            const confirmBtn = card.querySelector('#gm-txp-confirm');
            const cancelBtn = card.querySelector('#gm-txp-cancel');
            let settled = false;
            const cleanupFns = [];

            const cleanup = () => {
                overlay.classList.remove('show');
                document.body.style.overflow = '';
                document.removeEventListener('keydown', onKey);
                overlay.removeEventListener('click', onOverlayClick);
                while (cleanupFns.length) {
                    try { cleanupFns.pop()(); } catch (_) { /* best-effort cleanup */ }
                }
            };
            const accept = (e) => {
                if (e) {
                    e.preventDefault();
                    e.stopPropagation();
                }
                if (settled) return;
                settled = true;
                cleanup();
                resolve(true);
            };
            const reject_ = (e) => {
                if (e) {
                    e.preventDefault();
                    e.stopPropagation();
                }
                if (settled) return;
                settled = true;
                cleanup();
                reject(new Cancelled());
            };
            // Mobile wallet webviews (MiniPay/Opera in particular) can swallow
            // synthetic `click` events after an overlay/button state transition.
            // Bind pointer/touch activation as well as click so the preview's
            // Confirm button reliably continues GoodReserve/Uniswap flows.
            const bindActivation = (el, handler) => {
                if (!el) return;
                const events = ['pointerup', 'touchend', 'click'];
                events.forEach((name) => {
                    el.addEventListener(name, handler, { passive: false });
                    cleanupFns.push(() => el.removeEventListener(name, handler));
                });
            };
            // Security-critical dialog: deliberately do NOT bind Enter to
            // accept. A user who holds Enter to fire the upstream "Swap"
            // button could keyrepeat into auto-confirming the preview, which
            // is the exact blind-signing pattern this dialog is meant to
            // prevent. Escape cancels; everything else requires an explicit
            // mouse/touch/pointer activation on the Confirm button.
            const onKey = (e) => {
                if (e.key === 'Escape') reject_(e);
            };
            // Outside-click cancels. Must NOT use { once: true } — clicks
            // bubbling up from non-button content inside the card would
            // otherwise consume the listener without dismissing, leaving the
            // dialog only closable via Escape/Cancel.
            const onOverlayClick = (e) => {
                if (e.target === overlay) reject_(e);
            };
            bindActivation(confirmBtn, accept);
            bindActivation(cancelBtn, reject_);
            document.addEventListener('keydown', onKey);
            overlay.addEventListener('click', onOverlayClick);
        });
    }

    /**
     * Detect whether a thrown error is the user cancelling our preview, so
     * callers can swallow it without surfacing a misleading "transaction
     * failed" toast.
     */
    function isCancelled(err) {
        return err && err.name === 'GMTxPreviewCancelled';
    }

    root.GMTxPreview = { confirm, isCancelled, Cancelled };
})(window);
