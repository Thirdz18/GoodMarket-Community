/**
 * GoodMarket Unified Wallet Provider
 * ==================================
 * A unified EIP-1193 provider that works with:
 * - Privy connected wallets
 * - Injected wallets (MetaMask, MiniPay, etc.)
 * - Privy embedded wallets
 * 
 * Usage:
 *   const provider = await getUnifiedProvider();
 *   const accounts = await provider.request({ method: 'eth_accounts' });
 *   const signature = await provider.request({ method: 'personal_sign', params: [message, address] });
 * 
 * This replaces the old wc-bridge.js complexity with a simple unified interface.
 */

(function(global) {
    "use strict";

    // Prevent multiple instances
    if (global.GoodMarketProvider) return;

    // Configuration
    var _config = {
        // RPC URL for Celo
        rpcUrl: "https://forno.celo.org",
        chainId: 42220, // Celo Mainnet
        chainHex: "0xa4ec",
        // Privy app ID (optional - for embedded wallets)
        privyAppId: null,
        // Login method from session
        loginMethod: null,
        // Log function for debugging
        log: function() {}
    };

    // State
    var _state = {
        // Cached provider instance
        _provider: null,
        // Currently connected address
        address: null,
        // Provider type: 'privy', 'injected', 'embedded'
        providerType: null,
    };

    /**
     * Configure the provider
     */
    function configure(opts) {
        opts = opts || {};
        _config.rpcUrl = opts.rpcUrl || _config.rpcUrl;
        _config.chainId = opts.chainId || _config.chainId;
        _config.chainHex = opts.chainHex || _config.chainHex;
        _config.privyAppId = opts.privyAppId || _config.privyAppId;
        _config.loginMethod = opts.loginMethod || _config.loginMethod;
        _config.log = opts.log || function() {};

        _config.log('[Provider] Configured:', _config);
        return _provider;
    }

    /**
     * Get the unified provider
     * Priority: Privy > Injected > Embedded
     */
    async function getProvider() {
        if (_state._provider) {
            return _state._provider;
        }

        // 1. Try Privy embedded wallet first (if available)
        if (window.Privy && window.Privy.getWalletProvider) {
            try {
                var privyProvider = await window.Privy.getWalletProvider();
                if (privyProvider) {
                    _state._provider = privyProvider;
                    _state.providerType = 'privy';
                    _config.log('[Provider] Using Privy provider');
                    return privyProvider;
                }
            } catch(e) {
                _config.log('[Provider] Privy provider error:', e);
            }
        }

        // 2. Try injected wallet (MetaMask, MiniPay, etc.)
        if (window.ethereum) {
            _state._provider = window.ethereum;
            _state.providerType = 'injected';
            _config.log('[Provider] Using injected wallet');
            return window.ethereum;
        }

        // 3. Try Privy frame (embedded wallet iframe)
        if (window.Privy && window.Privy.openWallet) {
            _state.providerType = 'embedded';
            _config.log('[Provider] Using embedded wallet');
            return createEmbeddedWalletProvider();
        }

        throw new Error('No wallet provider available');
    }

    /**
     * Create a provider wrapper for embedded wallet
     */
    function createEmbeddedWalletProvider() {
        return {
            isEmbedded: true,
            request: async function(args) {
                var method = args.method;
                var params = args.params || [];

                switch(method) {
                    case 'eth_accounts':
                        // Return current connected address from session
                        return _state.address ? [_state.address] : [];

                    case 'eth_chainId':
                        return _config.chainHex;

                    case 'eth_requestAccounts':
                        // Open Privy wallet UI
                        if (window.Privy && window.Privy.openWallet) {
                            var result = await window.Privy.openWallet();
                            if (result && result.address) {
                                _state.address = result.address;
                                return [result.address];
                            }
                        }
                        throw new Error('User rejected connection');

                    case 'personal_sign':
                        // Sign message via Privy
                        if (window.Privy && window.Privy.signMessage) {
                            var sig = await window.Privy.signMessage(params[0], params[1]);
                            return sig;
                        }
                        throw new Error('Signing not available');

                    case 'eth_sendTransaction':
                        // Send transaction via Privy
                        if (window.Privy && window.Privy.sendTransaction) {
                            var txHash = await window.Privy.sendTransaction(params[0]);
                            return txHash;
                        }
                        throw new Error('Transaction sending not available');

                    default:
                        throw new Error('Method not supported: ' + method);
                }
            },
            on: function(event, callback) {
                // Stub for event listener compatibility
                _config.log('[Provider] Event listener for:', event);
            },
            removeListener: function(event, callback) {
                // Stub for event listener removal
            }
        };
    }

    /**
     * Check if a provider is available
     */
    function isAvailable() {
        return !!(window.Privy || window.ethereum);
    }

    /**
     * Check if currently connected
     */
    function isConnected() {
        return !!_state.address;
    }

    /**
     * Get current address
     */
    function getAddress() {
        return _state.address;
    }

    /**
     * Get provider type
     */
    function getProviderType() {
        return _state.providerType;
    }

    /**
     * Connect to wallet
     */
    async function connect() {
        var provider = await getProvider();
        
        if (provider.isEmbedded) {
            // For embedded wallet, call eth_requestAccounts
            var accounts = await provider.request({ method: 'eth_requestAccounts' });
            if (accounts && accounts.length > 0) {
                _state.address = accounts[0];
                return accounts[0];
            }
        } else {
            // For regular providers
            var accounts = await provider.request({ method: 'eth_requestAccounts' });
            if (accounts && accounts.length > 0) {
                _state.address = accounts[0];
                return accounts[0];
            }
        }
        
        throw new Error('Failed to connect');
    }

    /**
     * Disconnect
     */
    function disconnect() {
        _state.address = null;
        _state._provider = null;
        _state.providerType = null;
    }

    /**
     * Request method wrapper
     */
    async function request(args) {
        var provider = await getProvider();
        return provider.request(args);
    }

    /**
     * Set current address (from session)
     */
    function setAddress(address) {
        _state.address = address;
    }

    /**
     * Reset provider state
     */
    function reset() {
        _state._provider = null;
        _state.address = null;
        _state.providerType = null;
    }

    // Export API
    global.GoodMarketProvider = {
        configure: configure,
        getProvider: getProvider,
        isAvailable: isAvailable,
        isConnected: isConnected,
        getAddress: getAddress,
        getProviderType: getProviderType,
        connect: connect,
        disconnect: disconnect,
        request: request,
        setAddress: setAddress,
        reset: reset,
        // Backward compatibility
        isPreferred: function() {
            return _config.loginMethod === 'privy';
        }
    };

})(typeof window !== "undefined" ? window : this);
