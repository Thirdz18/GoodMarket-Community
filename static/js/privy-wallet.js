/**
 * GoodMarket × Privy Embedded Wallet Integration
 * ================================================
 * Handles wallet creation (email/Google/social login) via Privy.
 * The user gets a server-controlled embedded wallet — no seed phrase.
 */

(function () {
    'use strict';

    // ── Config ──────────────────────────────────────────────────────────────
    var PRIVY_APP_ID = (window.__GM_PRIVY_CONFIG__ && window.__GM_PRIVY_CONFIG__.appId) || '';
    var CELO_RPC    = (window.__GM_PRIVY_CONFIG__ && window.__GM_PRIVY_CONFIG__.rpcUrl) || 'https://forno.celo.org';
    var EXPLORER    = (window.__GM_PRIVY_CONFIG__ && window.__GM_PRIVY_CONFIG__.explorer) || 'https://celoscan.io';

    // ── SDK singleton ───────────────────────────────────────────────────────
    var _sdk         = null;
    var _initialized = false;
    var _initPromise = null;
    var _user        = null;

    // ── Public API ──────────────────────────────────────────────────────────

    function isConfigured() {
        return !!PRIVY_APP_ID;
    }

    function _ensureInit() {
        if (_initialized) return Promise.resolve(_sdk);
        if (_initPromise) return _initPromise;

        _initPromise = new Promise(function (resolve, reject) {
            // Load Privy script from jsdelivr CDN (more reliable)
            if (!document.getElementById('privy-sdk-script')) {
                var script = document.createElement('script');
                script.id  = 'privy-sdk-script';
                script.src = 'https://cdn.jsdelivr.net/npm/@privy-io/react-auth@1.72.2/dist/iframe/parent/privy-ui-kit.iife.js';
                script.onload = function () { initSdk(resolve, reject); };
                script.onerror = function () {
                    // Fallback to unpkg
                    var fallback = document.createElement('script');
                    fallback.id  = 'privy-sdk-script';
                    fallback.src = 'https://unpkg.com/@privy-io/react-auth@1.72.2/dist/iframe/parent/privy-ui-kit.iife.js';
                    fallback.onload = function () { initSdk(resolve, reject); };
                    fallback.onerror = function () { reject(new Error('Failed to load Privy SDK from CDN.')); };
                    document.head.appendChild(fallback);
                };
                document.head.appendChild(script);
            } else {
                initSdk(resolve, reject);
            }
        });
        return _initPromise;
    }

    function initSdk(resolve, reject) {
        try {
            var PrivyUIKit = window.PrivyUIKit;
            if (!PrivyUIKit) {
                reject(new Error('Privy UI Kit not found after script load.'));
                return;
            }

            var embeddedWalletConfig = {
                createWallet: true,
                noPromptOnSignature: false,
                embeddedWalletChainType: {
                    chainType: 'CELO',
                    chainId: 42220
                }
            };

            _sdk = PrivyUIKit.init({
                appId: PRIVY_APP_ID,
                embeddedWalletConfig: embeddedWalletConfig,
                appearance: {
                    theme: 'dark',
                    accentColor: '#7c3aed',
                    logo: 'https://goodmarket.live/static/icons/goodmarket-icon.png'
                },
                loginMethods: [
                    { method: 'email', name: 'Email' },
                    { method: 'google', name: 'Google' },
                    { method: 'twitter', name: 'X (Twitter)' },
                    { method: 'apple', name: 'Apple' },
                    { method: 'phone', name: 'Phone' }
                ],
                supportedChains: [42220, 44787]
            });

            _initialized = true;
            resolve(_sdk);
        } catch (err) {
            reject(err);
        }
    }

    function login() {
        return _ensureInit().then(function () {
            return new Promise(function (resolve, reject) {
                try {
                    if (!_sdk) {
                        reject(new Error('Privy SDK not ready.'));
                        return;
                    }

                    _sdk.login({
                        loginMethod: 'email',
                        embeddedWalletProvider: 'PRIVY_EMBEDDED_WALLET'
                    }).then(function (user) {
                        _user = user;
                        var wallet = _getEmbeddedWallet(user);
                        if (!wallet) {
                            reject(new Error('No embedded wallet found for this user.'));
                            return;
                        }
                        resolve({ address: wallet.address });
                    }).catch(function (err) {
                        reject(err);
                    });
                } catch (err) {
                    reject(err);
                }
            });
        });
    }

    function signMessage(message) {
        return _ensureInit().then(function () {
            return new Promise(function (resolve, reject) {
                if (!_sdk) {
                    reject(new Error('Privy SDK not ready.'));
                    return;
                }

                var wallet = _getEmbeddedWallet(_user);
                if (!wallet) {
                    reject(new Error('No embedded wallet connected.'));
                    return;
                }

                _sdk.signMessage({
                    message: message,
                    address: wallet.address
                }).then(function (result) {
                    resolve(result.signature);
                }).catch(function (err) {
                    reject(err);
                });
            });
        });
    }

    function signTransaction(txParams) {
        return _ensureInit().then(function () {
            return new Promise(function (resolve, reject) {
                if (!_sdk) {
                    reject(new Error('Privy SDK not ready.'));
                    return;
                }

                var wallet = _getEmbeddedWallet(_user);
                if (!wallet) {
                    reject(new Error('No embedded wallet connected.'));
                    return;
                }

                _sdk.sendTransaction({
                    to: txParams.to,
                    data: txParams.data || '0x',
                    value: txParams.value || '0x0',
                    gasLimit: txParams.gasLimit,
                    gasPrice: txParams.gasPrice,
                    address: wallet.address,
                    chainId: 42220
                }).then(function (result) {
                    resolve({ txHash: result.hash });
                }).catch(function (err) {
                    reject(err);
                });
            });
        });
    }

    function getUser() {
        return _user;
    }

    function _getEmbeddedWallet(user) {
        if (!user) return null;
        try {
            var wallets = user.walletWallets || [];
            for (var i = 0; i < wallets.length; i++) {
                var w = wallets[i];
                if (w.connectorType === 'PRIVY_EMBEDDED_WALLET' || w.type === 'embedded') {
                    return w;
                }
            }
            return wallets[0] || null;
        } catch (e) {
            return null;
        }
    }

    function exportPrivateKey() {
        return _ensureInit().then(function () {
            return new Promise(function (resolve, reject) {
                if (!_sdk) {
                    reject(new Error('Privy SDK not ready.'));
                    return;
                }

                var wallet = _getEmbeddedWallet(_user);
                if (!wallet) {
                    reject(new Error('No embedded wallet to export.'));
                    return;
                }

                _sdk.exportWallet({
                    address: wallet.address,
                    exportWalletType: 'embedded'
                }).then(function (result) {
                    resolve(result.privateKey);
                }).catch(function (err) {
                    reject(err);
                });
            });
        });
    }

    function logout() {
        if (_sdk && _sdk.logout) {
            _sdk.logout().then(function () {
                _user = null;
                _initialized = false;
                _sdk = null;
                _initPromise = null;
            });
        } else {
            _user = null;
            _initialized = false;
            _sdk = null;
            _initPromise = null;
        }
    }

    function isLoggedIn() {
        return !!_user;
    }

    // ── Expose on window ────────────────────────────────────────────────────
    window.GMPrivy = {
        isConfigured:      isConfigured,
        login:             login,
        signMessage:       signMessage,
        signTransaction:   signTransaction,
        exportPrivateKey:  exportPrivateKey,
        logout:            logout,
        getUser:           getUser,
        isLoggedIn:        isLoggedIn
    };

})();