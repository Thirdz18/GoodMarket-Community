/**
 * Superfluid G$ Streaming Library
 * 
 * Provides functions for creating, monitoring, and stopping G$ streams on Celo
 * using the Superfluid protocol.
 * 
 * Superfluid streams allow continuous payment of G$ tokens at a configurable flow rate.
 * Flow rate is expressed in G$/second (converted to wei/second internally)
 */

// Superfluid Configuration for Celo
const SUPERFLUID_CONFIG = {
    // Celo Mainnet
    hostAddress: '0xEB796bdb90fFA0da2d5c532F2bA53Fb15E59344b',
    // cDAI proxy on Celo (used for wrapping G$)
    cdaProxyFactory: '0xE7e898A3933d232461EbB0D863677808379FAb9e',
    // resolverAddress: '0x85998f8F8B0C69CBE8F31F56C7A5C79E16a7dF59',
};

// Superfluid SDK URLs
const SUPERFLUID_SDK_JS_URL = 'https://cdn.jsdelivr.net/npm/@superfluid-finance/js-sdk@0.6.8/dist/all.modules.js';
const SUPERFLUID_WEB3_URL = 'https://cdn.jsdelivr.net/npm/web3@1.10.0/dist/web3.min.js';

// G$ Token address on Celo
const G_DOLLAR_ADDRESS = '0x62B8B1e6C6D2fE5E1b7aD1b4E0D5c3B2F1e8D9A0C';

// Global state
let _sf = null;
let _web3 = null;
let _isInitialized = false;
let _userAddress = null;

/**
 * Load Superfluid SDK and Web3 dependencies
 */
async function loadSuperfluidDeps() {
    if (_isInitialized) return true;
    
    try {
        // Load Web3 if not already loaded
        if (typeof Web3 === 'undefined') {
            await loadScript(SUPERFLUID_WEB3_URL);
        }
        
        // Load Superfluid SDK
        if (typeof SuperfluidSDK === 'undefined') {
            await loadScript(SUPERFLUID_SDK_JS_URL);
        }
        
        _isInitialized = true;
        console.log('[Superfluid] Dependencies loaded');
        return true;
    } catch (err) {
        console.error('[Superfluid] Failed to load dependencies:', err);
        return false;
    }
}

/**
 * Initialize Superfluid SDK with user wallet
 * @param {Object} ethereumProvider - EIP-1193 provider (window.ethereum or WC provider)
 * @param {string} userAddress - User's wallet address
 */
async function initSuperfluid(ethereumProvider, userAddress) {
    if (!await loadSuperfluidDeps()) {
        throw new Error('Failed to load Superfluid dependencies');
    }
    
    _userAddress = userAddress;
    
    try {
        // Initialize Web3
        _web3 = new Web3(ethereumProvider);
        
        // Initialize Superfluid SDK
        const sfConfig = {
            chainId: 42220, // Celo mainnet
            provider: _web3.currentProvider,
            multiTokenWrapper: {
                parentToken: G_DOLLAR_ADDRESS,
                childToken: G_DOLLAR_ADDRESS, // G$ is both parent and child on Celo
            }
        };
        
        _sf = new SuperfluidSDK.Framework(sfConfig);
        await _sf.initialize();
        
        console.log('[Superfluid] Initialized for address:', userAddress);
        return true;
    } catch (err) {
        console.error('[Superfluid] Initialization failed:', err);
        _sf = null;
        return false;
    }
}

/**
 * Get Superfluid Framework instance
 */
function getSuperfluid() {
    return _sf;
}

/**
 * Get Web3 instance
 */
function getWeb3() {
    return _web3;
}

/**
 * Check if Superfluid is initialized
 */
function isSuperfluidReady() {
    return _sf !== null;
}

/**
 * Create a G$ stream to a recipient
 * @param {string} recipientAddress - Address to stream G$ to
 * @param {number} flowRate - Flow rate in G$ per second
 * @returns {Object} Transaction result
 */
async function createStream(recipientAddress, flowRate) {
    if (!_sf || !_userAddress) {
        throw new Error('Superfluid not initialized');
    }
    
    // Validate addresses
    if (!_web3.utils.isAddress(recipientAddress)) {
        throw new Error('Invalid recipient address');
    }
    
    if (_web3.utils.toChecksumAddress(recipientAddress) === _web3.utils.toChecksumAddress(_userAddress)) {
        throw new Error('Cannot stream to yourself');
    }
    
    // Convert G$/second to wei/second
    // G$ has 18 decimals like ETH
    const flowRateWei = _web3.utils.toWei(flowRate.toString(), 'ether');
    const flowRateNum = parseInt(flowRateWei);
    
    if (flowRateNum <= 0) {
        throw new Error('Flow rate must be greater than 0');
    }
    
    try {
        const cfa = _sf.cfa;
        
        // Create stream using Constant Flow Agreement
        const createFlowOp = cfa.createFlow({
            sender: _userAddress,
            receiver: recipientAddress,
            superToken: G_DOLLAR_ADDRESS,
            flowRate: flowRateNum.toString(),
        });
        
        const result = await createFlowOp.exec(_web3.currentProvider);
        
        console.log('[Superfluid] Stream created:', {
            recipient: recipientAddress,
            flowRate: flowRate,
            flowRateWei: flowRateWei,
            txHash: result.hash,
        });
        
        return {
            success: true,
            txHash: result.hash,
            recipient: recipientAddress,
            flowRate: flowRate,
        };
    } catch (err) {
        console.error('[Superfluid] Failed to create stream:', err);
        throw new Error('Failed to create stream: ' + (err.message || 'Unknown error'));
    }
}

/**
 * Update an existing stream's flow rate
 * @param {string} recipientAddress - Address receiving the stream
 * @param {number} newFlowRate - New flow rate in G$ per second
 */
async function updateStream(recipientAddress, newFlowRate) {
    if (!_sf || !_userAddress) {
        throw new Error('Superfluid not initialized');
    }
    
    const flowRateWei = _web3.utils.toWei(newFlowRate.toString(), 'ether');
    const flowRateNum = parseInt(flowRateWei);
    
    try {
        const cfa = _sf.cfa;
        
        const updateFlowOp = cfa.updateFlow({
            sender: _userAddress,
            receiver: recipientAddress,
            superToken: G_DOLLAR_ADDRESS,
            flowRate: flowRateNum.toString(),
        });
        
        const result = await updateFlowOp.exec(_web3.currentProvider);
        
        console.log('[Superfluid] Stream updated:', {
            recipient: recipientAddress,
            newFlowRate: newFlowRate,
            txHash: result.hash,
        });
        
        return {
            success: true,
            txHash: result.hash,
            recipient: recipientAddress,
            flowRate: newFlowRate,
        };
    } catch (err) {
        console.error('[Superfluid] Failed to update stream:', err);
        throw new Error('Failed to update stream: ' + (err.message || 'Unknown error'));
    }
}

/**
 * Delete/stop a G$ stream
 * @param {string} recipientAddress - Address receiving the stream to stop
 */
async function deleteStream(recipientAddress) {
    if (!_sf || !_userAddress) {
        throw new Error('Superfluid not initialized');
    }
    
    try {
        const cfa = _sf.cfa;
        
        const deleteFlowOp = cfa.deleteFlow({
            sender: _userAddress,
            receiver: recipientAddress,
            superToken: G_DOLLAR_ADDRESS,
        });
        
        const result = await deleteFlowOp.exec(_web3.currentProvider);
        
        console.log('[Superfluid] Stream deleted:', {
            recipient: recipientAddress,
            txHash: result.hash,
        });
        
        return {
            success: true,
            txHash: result.hash,
            recipient: recipientAddress,
        };
    } catch (err) {
        console.error('[Superfluid] Failed to delete stream:', err);
        throw new Error('Failed to delete stream: ' + (err.message || 'Unknown error'));
    }
}

/**
 * Get flow information for a stream
 * @param {string} sender - Sender address
 * @param {string} receiver - Receiver address
 */
async function getFlowInfo(sender, receiver) {
    if (!_sf) {
        throw new Error('Superfluid not initialized');
    }
    
    try {
        const cfa = _sf.cfa;
        const flowInfo = await cfa.getFlow({
            superToken: G_DOLLAR_ADDRESS,
            sender: sender,
            receiver: receiver,
            provider: _web3.currentProvider,
        });
        
        // Convert from wei/second to G$/second
        const flowRateG$ = parseFloat(_web3.utils.fromWei(flowInfo.flowRate));
        
        return {
            exists: flowInfo.exists,
            flowRate: flowRateG$,
            deposit: _web3.utils.fromWei(flowInfo.deposit),
            owedDeposit: _web3.utils.fromWei(flowInfo.owedDeposit),
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
    if (!_sf || !_userAddress) {
        return [];
    }
    
    try {
        // Get list of accounts that have received streams from this user
        // This requires indexing - for now, we'll track locally
        const streams = [];
        const storedStreams = getStoredStreams();
        
        for (const recipient of Object.keys(storedStreams)) {
            const flowInfo = await getFlowInfo(_userAddress, recipient);
            if (flowInfo.exists && flowInfo.flowRate > 0) {
                streams.push({
                    recipient: recipient,
                    flowRate: flowInfo.flowRate,
                    alias: storedStreams[recipient].alias || '',
                    startTime: storedStreams[recipient].startTime,
                });
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
    if (!_sf || !_userAddress) {
        return [];
    }
    
    try {
        // This would typically require an indexer or subgraph query
        // For now, return empty - would need external indexing service
        console.log('[Superfluid] Incoming streams require external indexing');
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
    streams[recipientAddress] = {
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
    delete streams[recipientAddress];
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
        script.onerror = reject;
        document.head.appendChild(script);
    });
}

/**
 * Format flow rate for display
 * @param {number} flowRate - Flow rate in G$/second
 */
function formatFlowRate(flowRate) {
    if (flowRate < 0.001) {
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