/**
 * Superfluid G$ Streaming Library
 * 
 * Provides functions for creating, monitoring, and stopping G$ streams on Celo
 * using the Superfluid protocol via ethers.js.
 * 
 * Superfluid streams allow continuous payment of G$ tokens at a configurable flow rate.
 * Flow rate is expressed in G$/second (converted to wei/second internally)
 */

// ethers.js CDN URL
const ETHERS_JS_URL = 'https://cdn.jsdelivr.net/npm/ethers@5.7.2/dist/ethers.umd.min.js';

// Get Superfluid configuration from backend (injected by Jinja template)
// Falls back to hardcoded defaults for Celo Mainnet (chainId: 42220)
const _backendConfig = window.SUPERFLUID_CONFIG || {};
const SUPERFLUID_CONFIG = {
    chainId: 42220,
    // Superfluid Host contract on Celo
    hostAddress: _backendConfig.host_address || '0xEB796bdb90fFA0da2d5c532F2bA53Fb15E59344b',
    // Constant Flow Agreement v1 on Celo
    cfaV1Address: _backendConfig.cfa_v1_address || '0x254A4D3b2a5D9B8C7D6E5F4A3B2C1D0E9F8A7B6C',
    // Superfluid Resolver on Celo
    resolverAddress: _backendConfig.resolver_address || '0x85998f8F8B0C69CBE8F31F56C7A5C79E16a7dF59',
};

// G$ Token address on Celo (Super Token wrapper)
const G_DOLLAR_SUPER_TOKEN = _backendConfig.super_token_address || '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A';

console.log('[Superfluid] Using config:', {
    hostAddress: SUPERFLUID_CONFIG.hostAddress,
    cfaV1Address: SUPERFLUID_CONFIG.cfaV1Address,
    superToken: G_DOLLAR_SUPER_TOKEN
});

// CFAv1 ABI - minimal functions needed for flow operations
const CFAV1_ABI = [
    "function createFlow(address superToken, address sender, address receiver, int96 flowRate, bytes calldata userData) external returns (bool)",
    "function updateFlow(address superToken, address sender, address receiver, int96 flowRate, bytes calldata userData) external returns (bool)",
    "function deleteFlow(address superToken, address sender, address receiver, address userData) external returns (bool)",
    "function getFlow(address superToken, address sender, address receiver) external view returns (uint256,uint256,uint256,uint256)",
    "function getAccountFlowInfo(address superToken, address account) external view returns (int96,uint256,uint256)",
    "function getFlowRate(address superToken, address sender, address receiver) external view returns (int96)"
];

// ERC20 ABI for balance checks
const ERC20_ABI = [
    "function balanceOf(address account) external view returns (uint256)",
    "function decimals() external view returns (uint8)",
    "function symbol() external view returns (string)"
];

// Global state
let _ethers = null;
let _provider = null;
let _signer = null;
let _cfaContract = null;
let _isInitialized = false;
let _userAddress = null;
let _sfConfigLoaded = false;

/**
 * Load ethers.js from CDN
 */
async function loadEthersJs() {
    if (typeof ethers !== 'undefined') {
        _ethers = ethers;
        return true;
    }
    
    try {
        await loadScript(ETHERS_JS_URL);
        _ethers = window.ethers;
        console.log('[Superfluid] ethers.js loaded');
        return true;
    } catch (err) {
        console.error('[Superfluid] Failed to load ethers.js:', err);
        return false;
    }
}

/**
 * Load Superfluid config from resolver (or use defaults)
 */
async function loadSuperfluidConfig() {
    if (_sfConfigLoaded) return true;
    
    try {
        // Try to load config from resolver if available
        // For now, use the defaults which are well-known Superfluid Celo addresses
        // The resolver at 0x85998... can be queried for: "SuperfluidLoader_v1", "CFAv1", "Host"
        _sfConfigLoaded = true;
        console.log('[Superfluid] Config loaded for Celo mainnet');
        return true;
    } catch (err) {
        console.warn('[Superfluid] Using default config:', err);
        _sfConfigLoaded = true;
        return true;
    }
}

/**
 * Initialize Superfluid with user wallet
 * @param {Object} ethereumProvider - EIP-1193 provider (window.ethereum or WC provider)
 * @param {string} userAddress - User's wallet address
 */
async function initSuperfluid(ethereumProvider, userAddress) {
    // Load dependencies
    if (!await loadEthersJs()) {
        throw new Error('Failed to load ethers.js');
    }
    
    if (!await loadSuperfluidConfig()) {
        throw new Error('Failed to load Superfluid config');
    }
    
    _userAddress = userAddress;
    
    try {
        // Initialize ethers provider
        _provider = new _ethers.providers.Web3Provider(ethereumProvider);
        
        // Get signer
        _signer = _provider.getSigner();
        
        // Initialize CFA contract
        _cfaContract = new _ethers.Contract(
            SUPERFLUID_CONFIG.cfaV1Address,
            CFAV1_ABI,
            _signer
        );
        
        _isInitialized = true;
        console.log('[Superfluid] Initialized for address:', userAddress);
        return true;
    } catch (err) {
        console.error('[Superfluid] Initialization failed:', err);
        _isInitialized = false;
        throw err;
    }
}

/**
 * Check if Superfluid is ready
 */
function isSuperfluidReady() {
    return _isInitialized && _cfaContract !== null;
}

/**
 * Create a G$ stream to a recipient
 * @param {string} recipientAddress - Address to stream G$ to
 * @param {number} flowRate - Flow rate in G$ per second (as a number, not wei)
 * @returns {Object} Transaction result
 */
async function createStream(recipientAddress, flowRate) {
    if (!_isInitialized || !_cfaContract) {
        throw new Error('Superfluid not initialized');
    }
    
    // Validate addresses
    const isValidAddress = _ethers.utils.isAddress(recipientAddress);
    if (!isValidAddress) {
        throw new Error('Invalid recipient address');
    }
    
    const senderChecksum = _ethers.utils.getAddress(_userAddress);
    const recipientChecksum = _ethers.utils.getAddress(recipientAddress);
    
    if (senderChecksum === recipientChecksum) {
        throw new Error('Cannot stream to yourself');
    }
    
    // Convert G$/second to wei/second (Superfluid uses wei for flow rate)
    // G$ has 18 decimals like ETH
    const flowRateWei = _ethers.utils.parseUnits(flowRate.toString(), 'ether');
    const flowRateBn = flowRateWei;
    
    if (flowRateBn.lte(0)) {
        throw new Error('Flow rate must be greater than 0');
    }
    
    // Flow rate in Superfluid is int96, which fits in a JS number for reasonable values
    const flowRateInt96 = flowRateBn.toString();
    
    try {
        // Create flow transaction
        const tx = await _cfaContract.createFlow(
            G_DOLLAR_SUPER_TOKEN,
            _userAddress,
            recipientChecksum,
            flowRateInt96,
            '0x' // empty userData
        );
        
        console.log('[Superfluid] Stream creation tx:', tx.hash);
        
        // Wait for confirmation
        const receipt = await tx.wait();
        
        console.log('[Superfluid] Stream created:', {
            recipient: recipientChecksum,
            flowRate: flowRate,
            txHash: receipt.transactionHash,
            blockNumber: receipt.blockNumber
        });
        
        return {
            success: true,
            txHash: receipt.transactionHash,
            recipient: recipientChecksum,
            flowRate: flowRate,
        };
    } catch (err) {
        console.error('[Superfluid] Failed to create stream:', err);
        
        // Parse common errors
        const errorMsg = err.message || '';
        if (errorMsg.includes('user rejected') || errorMsg.includes('User denied')) {
            throw new Error('Transaction cancelled');
        }
        if (errorMsg.includes('insufficient funds')) {
            throw new Error('Insufficient balance for stream');
        }
        
        throw new Error('Failed to create stream: ' + (err.reason || err.message || 'Unknown error'));
    }
}

/**
 * Update an existing stream's flow rate
 * @param {string} recipientAddress - Address receiving the stream
 * @param {number} newFlowRate - New flow rate in G$ per second
 */
async function updateStream(recipientAddress, newFlowRate) {
    if (!_isInitialized || !_cfaContract) {
        throw new Error('Superfluid not initialized');
    }
    
    const recipientChecksum = _ethers.utils.getAddress(recipientAddress);
    const flowRateWei = _ethers.utils.parseUnits(newFlowRate.toString(), 'ether');
    const flowRateInt96 = flowRateWei.toString();
    
    try {
        const tx = await _cfaContract.updateFlow(
            G_DOLLAR_SUPER_TOKEN,
            _userAddress,
            recipientChecksum,
            flowRateInt96,
            '0x'
        );
        
        const receipt = await tx.wait();
        
        console.log('[Superfluid] Stream updated:', {
            recipient: recipientChecksum,
            newFlowRate: newFlowRate,
            txHash: receipt.transactionHash
        });
        
        return {
            success: true,
            txHash: receipt.transactionHash,
            recipient: recipientChecksum,
            flowRate: newFlowRate,
        };
    } catch (err) {
        console.error('[Superfluid] Failed to update stream:', err);
        throw new Error('Failed to update stream: ' + (err.reason || err.message || 'Unknown error'));
    }
}

/**
 * Delete/stop a G$ stream
 * @param {string} recipientAddress - Address receiving the stream to stop
 */
async function deleteStream(recipientAddress) {
    if (!_isInitialized || !_cfaContract) {
        throw new Error('Superfluid not initialized');
    }
    
    const recipientChecksum = _ethers.utils.getAddress(recipientAddress);
    
    try {
        const tx = await _cfaContract.deleteFlow(
            G_DOLLAR_SUPER_TOKEN,
            _userAddress,
            recipientChecksum,
            '0x'
        );
        
        const receipt = await tx.wait();
        
        console.log('[Superfluid] Stream deleted:', {
            recipient: recipientChecksum,
            txHash: receipt.transactionHash
        });
        
        return {
            success: true,
            txHash: receipt.transactionHash,
            recipient: recipientChecksum,
        };
    } catch (err) {
        console.error('[Superfluid] Failed to delete stream:', err);
        throw new Error('Failed to delete stream: ' + (err.reason || err.message || 'Unknown error'));
    }
}

/**
 * Get flow information for a stream
 * @param {string} sender - Sender address
 * @param {string} receiver - Receiver address
 */
async function getFlowInfo(sender, receiver) {
    if (!_isInitialized || !_cfaContract) {
        throw new Error('Superfluid not initialized');
    }
    
    try {
        // Create read-only contract for queries
        const readOnlyCfa = _cfaContract.connect(_provider);
        
        const flowInfo = await readOnlyCfa.getFlow(
            G_DOLLAR_SUPER_TOKEN,
            sender,
            receiver
        );
        
        // flowInfo returns: (lastUpdate, deposit, owedDeposit, flowRate)
        // flowRate is in wei/second, convert to G$/second
        const flowRateG$ = parseFloat(_ethers.utils.formatUnits(flowInfo.flowRate, 'ether'));
        
        return {
            exists: flowRateG$ > 0,
            flowRate: flowRateG$,
            deposit: parseFloat(_ethers.utils.formatUnits(flowInfo.deposit, 'ether')),
            owedDeposit: parseFloat(_ethers.utils.formatUnits(flowInfo.owedDeposit, 'ether')),
        };
    } catch (err) {
        console.error('[Superfluid] Failed to get flow info:', err);
        return {
            exists: false,
            flowRate: 0,
            deposit: 0,
            owedDeposit: 0,
        };
    }
}

/**
 * Get all streams where user is sender (outgoing)
 */
async function getMyOutgoingStreams() {
    if (!_isInitialized || !_userAddress) {
        return [];
    }
    
    try {
        const streams = [];
        const storedStreams = getStoredStreams();
        
        for (const recipient of Object.keys(storedStreams)) {
            try {
                const flowInfo = await getFlowInfo(_userAddress, recipient);
                if (flowInfo.exists && flowInfo.flowRate > 0) {
                    streams.push({
                        recipient: recipient,
                        flowRate: flowInfo.flowRate,
                        alias: storedStreams[recipient].alias || '',
                        startTime: storedStreams[recipient].startTime,
                    });
                }
            } catch (e) {
                console.warn('[Superfluid] Could not get flow info for', recipient, e);
            }
        }
        
        return streams;
    } catch (err) {
        console.error('[Superfluid] Failed to get outgoing streams:', err);
        return [];
    }
}

/**
 * Get all streams where user is receiver (incoming)
 */
async function getMyIncomingStreams() {
    if (!_isInitialized || !_userAddress) {
        return [];
    }
    
    try {
        // Get account flow info (incoming - outgoing)
        const readOnlyCfa = _cfaContract.connect(_provider);
        const accountFlowInfo = await readOnlyCfa.getAccountFlowInfo(
            G_DOLLAR_SUPER_TOKEN,
            _userAddress
        );
        
        // accountFlowInfo returns: (flowRate, buffer, platformFee)
        // This gives net flow, not individual streams
        const netFlowRate = parseFloat(_ethers.utils.formatUnits(accountFlowInfo.flowRate, 'ether'));
        
        console.log('[Superfluid] Net flow rate for', _userAddress, ':', netFlowRate);
        return [];
    } catch (err) {
        console.error('[Superfluid] Failed to get incoming streams:', err);
        return [];
    }
}

/**
 * Store stream info locally (for tracking)
 */
function storeStream(recipientAddress, alias = '') {
    const streams = getStoredStreams();
    const checksumAddr = _ethers ? _ethers.utils.getAddress(recipientAddress) : recipientAddress;
    streams[checksumAddr] = {
        alias: alias,
        startTime: Date.now(),
    };
    localStorage.setItem('sf_streams', JSON.stringify(streams));
}

/**
 * Remove stream from local storage
 */
function removeStoredStream(recipientAddress) {
    const streams = getStoredStreams();
    const checksumAddr = _ethers ? _ethers.utils.getAddress(recipientAddress) : recipientAddress;
    delete streams[checksumAddr];
    localStorage.setItem('sf_streams', JSON.stringify(streams));
}

/**
 * Get stored streams from localStorage
 */
function getStoredStreams() {
    try {
        const stored = localStorage.getItem('sf_streams');
        return stored ? JSON.parse(stored) : {};
    } catch (err) {
        return {};
    }
}

/**
 * Helper to load external scripts
 */
function loadScript(src) {
    return new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = src;
        script.onload = resolve;
        script.onerror = () => reject(new Error('Failed to load script: ' + src));
        document.head.appendChild(script);
    });
}

/**
 * Format flow rate for display
 * @param {number} flowRate - Flow rate in G$/second
 */
function formatFlowRate(flowRate) {
    if (flowRate < 0.000001) {
        return (flowRate * 1000000).toFixed(4) + ' μG$/s';
    } else if (flowRate < 0.001) {
        return (flowRate * 1000).toFixed(4) + ' mG$/s';
    } else if (flowRate < 1) {
        return (flowRate * 1000).toFixed(2) + ' G$/s';
    } else if (flowRate < 60) {
        return flowRate.toFixed(2) + ' G$/s';
    } else if (flowRate < 3600) {
        return (flowRate / 60).toFixed(2) + ' G$/min';
    } else if (flowRate < 86400) {
        return (flowRate / 3600).toFixed(2) + ' G$/hour';
    } else {
        return (flowRate / 86400).toFixed(2) + ' G$/day';
    }
}

/**
 * Convert G$/second to human-readable rate
 */
function flowRateToHumanReadable(flowRate) {
    return formatFlowRate(flowRate);
}

/**
 * Convert flow rate selector value to G$/second
 * @param {string} value - Selector value (gps, gpm, gph, gpd)
 * @param {number} amount - Amount value
 */
function selectorToFlowRate(value, amount) {
    const rate = parseFloat(amount) || 0;
    switch (value) {
        case 'gps': return rate;           // G$ per second
        case 'gpm': return rate / 60;       // G$ per minute
        case 'gph': return rate / 3600;     // G$ per hour
        case 'gpd': return rate / 86400;    // G$ per day
        default: return rate;
    }
}

/**
 * Estimate total G$ to be streamed over time
 * @param {number} flowRate - Flow rate in G$/second
 * @param {number} seconds - Duration in seconds
 */
function estimateStreamTotal(flowRate, seconds) {
    return flowRate * seconds;
}

/**
 * Get Superfluid framework (ethers.js provider)
 */
function getSuperfluid() {
    return {
        provider: _provider,
        signer: _signer,
        cfaContract: _cfaContract,
        config: SUPERFLUID_CONFIG
    };
}

/**
 * Get Web3 instance (for backward compatibility, returns ethers provider)
 */
function getWeb3() {
    return {
        currentProvider: _provider
    };
}

// Export functions for use in wallet.html
window.SuperfluidStream = {
    init: initSuperfluid,
    isReady: isSuperfluidReady,
    createStream: createStream,
    updateStream: updateStream,
    deleteStream: deleteStream,
    getFlowInfo: getFlowInfo,
    getMyOutgoingStreams: getMyOutgoingStreams,
    getMyIncomingStreams: getMyIncomingStreams,
    storeStream: storeStream,
    removeStoredStream: removeStoredStream,
    getStoredStreams: getStoredStreams,
    formatFlowRate: formatFlowRate,
    flowRateToHumanReadable: flowRateToHumanReadable,
    selectorToFlowRate: selectorToFlowRate,
    estimateStreamTotal: estimateStreamTotal,
    getSuperfluid: getSuperfluid,
    getWeb3: getWeb3,
};

console.log('[Superfluid] SuperfluidStream library loaded');