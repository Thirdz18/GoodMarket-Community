import http from 'http';
import { SignClient } from '@walletconnect/sign-client';
import { Turnkey } from '@turnkey/sdk-server';
import { encryptPrivateKeyToBundle } from '@turnkey/crypto';
import { decryptExportBundle, generateP256KeyPair } from '@turnkey/crypto';

const PROJECT_ID = process.env.WALLETCONNECT_PROJECT_ID;
const PORT = parseInt(process.env.WC_SERVICE_PORT || '3001');
const APP_URL = process.env.APP_URL || 'https://goodmarket.live';
const TURNKEY_OTP_APP_NAME = process.env.TURNKEY_OTP_APP_NAME || 'GoodMarket';

const TURNKEY_ORG_ID = process.env.TURNKEY_ORGANIZATION_ID;
const TURNKEY_API_PUBLIC_KEY = process.env.TURNKEY_API_PUBLIC_KEY;
const TURNKEY_API_PRIVATE_KEY = process.env.TURNKEY_API_PRIVATE_KEY;

if (!PROJECT_ID) {
    console.warn('WC_WARN: WALLETCONNECT_PROJECT_ID not set — WalletConnect QR disabled');
}

let turnkey = null;
if (TURNKEY_ORG_ID && TURNKEY_API_PUBLIC_KEY && TURNKEY_API_PRIVATE_KEY) {
    turnkey = new Turnkey({
        defaultOrganizationId: TURNKEY_ORG_ID,
        apiBaseUrl: 'https://api.turnkey.com',
        apiPrivateKey: TURNKEY_API_PRIVATE_KEY,
        apiPublicKey: TURNKEY_API_PUBLIC_KEY,
    });
    console.log('TURNKEY_READY');
} else {
    console.warn('WC_WARN: Turnkey credentials not set — Turnkey features disabled');
}

let signClient = null;
const sessions = {};
const otpSessionsByEmail = {};

function pruneExpiredOtpSessions() {
    const now = Date.now();
    for (const [email, item] of Object.entries(otpSessionsByEmail)) {
        if (!item || !item.expiresAt || item.expiresAt <= now) {
            delete otpSessionsByEmail[email];
        }
    }
}

async function initClient() {
    if (!PROJECT_ID) {
        console.warn('WC_WARN: Skipping WalletConnect init');
        return;
    }
    signClient = await SignClient.init({
        projectId: PROJECT_ID,
        metadata: {
            name: 'GoodMarket',
            description: 'Learn & Earn with GoodDollar on Celo',
            url: APP_URL,
            icons: [`${APP_URL}/static/icons/icon-192x192.png`]
        }
    });
    console.log('WC_SERVICE_READY');
}

function readBody(req) {
    return new Promise((resolve, reject) => {
        let body = '';
        req.on('data', chunk => { body += chunk; });
        req.on('end', () => {
            try { resolve(JSON.parse(body || '{}')); }
            catch (e) { reject(new Error('Invalid JSON')); }
        });
        req.on('error', reject);
    });
}

function sendJSON(res, statusCode, data) {
    res.writeHead(statusCode, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
}

async function handleTurnkey(req, res, path) {
    if (!turnkey) {
        return sendJSON(res, 503, { error: 'Turnkey not configured' });
    }
    const client = turnkey.apiClient();

    // POST /turnkey/create-wallet — create sub-org + Celo wallet for a new user
    if (req.method === 'POST' && path === '/turnkey/create-wallet') {
        try {
            const { userId, userName } = await readBody(req);
            if (!userId) return sendJSON(res, 400, { error: 'userId required' });

            const result = await client.createSubOrganization({
                organizationId: TURNKEY_ORG_ID,
                subOrganizationName: `GoodMarket-${userId}`,
                rootUsers: [{
                    userName: userName || userId,
                    apiKeys: [{
                        apiKeyName: 'GoodMarket Server Key',
                        publicKey: TURNKEY_API_PUBLIC_KEY,
                        curveType: 'API_KEY_CURVE_P256'
                    }],
                    authenticators: [],
                    oauthProviders: []
                }],
                rootQuorumThreshold: 1,
                wallet: {
                    walletName: 'GoodMarket Celo Wallet',
                    accounts: [{
                        curve: 'CURVE_SECP256K1',
                        pathFormat: 'PATH_FORMAT_BIP32',
                        path: "m/44'/60'/0'/0/0",
                        addressFormat: 'ADDRESS_FORMAT_ETHEREUM'
                    }]
                }
            });

            const subOrgId = result.subOrganizationId;
            const walletId = result.wallet?.walletId;

            const subClient = turnkey.apiClient();
            const accounts = await subClient.getWalletAccounts({
                organizationId: subOrgId,
                walletId
            });

            const address = accounts.accounts?.[0]?.address || null;
            return sendJSON(res, 200, { subOrgId, walletId, address });
        } catch (err) {
            console.error('Turnkey create-wallet error:', err.message);
            return sendJSON(res, 500, { error: err.message });
        }
    }

    // POST /turnkey/import-key — import an existing private key into a sub-org
    if (req.method === 'POST' && path === '/turnkey/import-key') {
        try {
            const { userId, userName, privateKey } = await readBody(req);
            if (!userId || !privateKey) return sendJSON(res, 400, { error: 'userId and privateKey required' });

            const normalizedKey = privateKey.startsWith('0x') ? privateKey.slice(2) : privateKey;

            // Step 1: Create sub-org for user (no wallet yet)
            const subOrgResult = await client.createSubOrganization({
                organizationId: TURNKEY_ORG_ID,
                subOrganizationName: `GoodMarket-${userId}`,
                rootUsers: [{
                    userName: userName || userId,
                    apiKeys: [{
                        apiKeyName: 'GoodMarket Server Key',
                        publicKey: TURNKEY_API_PUBLIC_KEY,
                        curveType: 'API_KEY_CURVE_P256'
                    }],
                    authenticators: [],
                    oauthProviders: []
                }],
                rootQuorumThreshold: 1
            });
            const subOrgId = subOrgResult.subOrganizationId;

            // Step 2: Init import to get the enclave's target public key
            const initResult = await client.initImportPrivateKey({
                organizationId: subOrgId,
                userId: subOrgResult.rootUserIds?.[0]
            });
            const importBundle = initResult.importBundle;

            // Step 3: Encrypt the private key using Turnkey's enclave key
            const encryptedBundle = await encryptPrivateKeyToBundle({
                privateKeyHex: normalizedKey,
                importBundle
            });

            // Step 4: Import the encrypted key
            const importResult = await client.importPrivateKey({
                organizationId: subOrgId,
                userId: subOrgResult.rootUserIds?.[0],
                keyName: 'GoodMarket Celo Key',
                encryptedBundle,
                curve: 'CURVE_SECP256K1',
                addressFormats: ['ADDRESS_FORMAT_ETHEREUM']
            });

            const privateKeyId = importResult.privateKeyId;
            const address = importResult.addresses?.[0]?.address || null;

            return sendJSON(res, 200, {
                subOrgId,
                privateKeyId,
                address,
                keyType: 'private_key'
            });
        } catch (err) {
            console.error('Turnkey import-key error:', err.message);
            return sendJSON(res, 500, { error: err.message });
        }
    }

    // POST /turnkey/sign-tx — sign an EVM transaction
    if (req.method === 'POST' && path === '/turnkey/sign-tx') {
        try {
            const { subOrgId, signWith, unsignedTx } = await readBody(req);
            if (!subOrgId || !signWith || !unsignedTx) {
                return sendJSON(res, 400, { error: 'subOrgId, signWith, unsignedTx required' });
            }

            const result = await client.signTransaction({
                organizationId: subOrgId,
                signWith,
                unsignedTransaction: unsignedTx.startsWith('0x') ? unsignedTx.slice(2) : unsignedTx,
                type: 'TRANSACTION_TYPE_ETHEREUM'
            });

            const signedTx = '0x' + result.signedTransaction;
            return sendJSON(res, 200, { signedTx });
        } catch (err) {
            console.error('Turnkey sign-tx error:', err.message);
            return sendJSON(res, 500, { error: err.message });
        }
    }

    // POST /turnkey/sign-msg — sign a personal message
    if (req.method === 'POST' && path === '/turnkey/sign-msg') {
        try {
            const { subOrgId, signWith, message } = await readBody(req);
            if (!subOrgId || !signWith || !message) {
                return sendJSON(res, 400, { error: 'subOrgId, signWith, message required' });
            }

            const msgHex = Buffer.from(message, 'utf8').toString('hex');
            const prefix = Buffer.from(`\x19Ethereum Signed Message:\n${message.length}`, 'utf8').toString('hex');
            const fullPayload = prefix + msgHex;

            const result = await client.signRawPayload({
                organizationId: subOrgId,
                signWith,
                payload: fullPayload,
                encoding: 'PAYLOAD_ENCODING_HEXADECIMAL',
                hashFunction: 'HASH_FUNCTION_KECCAK256'
            });

            const { r, s, v } = result;
            const signature = '0x' + r + s + (parseInt(v, 16) + 27).toString(16).padStart(2, '0');
            return sendJSON(res, 200, { signature });
        } catch (err) {
            console.error('Turnkey sign-msg error:', err.message);
            return sendJSON(res, 500, { error: err.message });
        }
    }

    // POST /turnkey/email/send-code — send OTP code to email via Turnkey
    if (req.method === 'POST' && (path === '/turnkey/email/send-code' || path === '/turnkey/email/send-otp' || path === '/turnkey/otp/send')) {
        try {
            pruneExpiredOtpSessions();
            const { email } = await readBody(req);
            const normalizedEmail = (email || '').toString().trim().toLowerCase();
            if (!normalizedEmail || !normalizedEmail.includes('@')) {
                return sendJSON(res, 400, { error: 'Valid email required' });
            }

            const result = await client.initOtp({
                contact: normalizedEmail,
                otpType: 'OTP_TYPE_EMAIL',
                appName: TURNKEY_OTP_APP_NAME,
                alphanumeric: false,
                otpLength: 6,
                expirationSeconds: '600'
            });

            if (!result?.otpId) {
                return sendJSON(res, 500, { error: 'Turnkey did not return otpId' });
            }

            otpSessionsByEmail[normalizedEmail] = {
                otpId: result.otpId,
                expiresAt: Date.now() + 10 * 60 * 1000
            };

            return sendJSON(res, 200, { success: true, otpId: result.otpId });
        } catch (err) {
            console.error('Turnkey email OTP send error:', err.message);
            return sendJSON(res, 500, { error: err.message });
        }
    }

    // POST /turnkey/email/verify-code — verify OTP code for email
    if (req.method === 'POST' && (path === '/turnkey/email/verify-code' || path === '/turnkey/email/verify-otp' || path === '/turnkey/otp/verify')) {
        try {
            pruneExpiredOtpSessions();
            const { email, code } = await readBody(req);
            const normalizedEmail = (email || '').toString().trim().toLowerCase();
            const otpCode = (code || '').toString().trim();
            if (!normalizedEmail || !otpCode) {
                return sendJSON(res, 400, { error: 'email and code required' });
            }

            const otpState = otpSessionsByEmail[normalizedEmail];
            if (!otpState?.otpId) {
                return sendJSON(res, 400, { error: 'No active OTP for email. Request a new code.' });
            }

            const result = await client.verifyOtp({
                otpId: otpState.otpId,
                otpCode
            });

            if (!result?.verificationToken) {
                return sendJSON(res, 400, { error: 'Invalid OTP code' });
            }

            delete otpSessionsByEmail[normalizedEmail];
            return sendJSON(res, 200, { success: true });
        } catch (err) {
            console.error('Turnkey email OTP verify error:', err.message);
            return sendJSON(res, 400, { error: err.message || 'Invalid OTP code' });
        }
    }

    // GET /turnkey/wallet/:suborg_id — get wallet address for sub-org
    if (req.method === 'GET' && path.startsWith('/turnkey/wallet/')) {
        try {
            const subOrgId = path.split('/turnkey/wallet/')[1];
            if (!subOrgId) return sendJSON(res, 400, { error: 'subOrgId required' });

            const walletsResult = await client.getWallets({ organizationId: subOrgId });
            if (!walletsResult.wallets?.length) {
                const keysResult = await client.getPrivateKeys({ organizationId: subOrgId });
                const key = keysResult.privateKeys?.[0];
                const address = key?.addresses?.[0]?.address || null;
                return sendJSON(res, 200, {
                    address,
                    signWith: key?.privateKeyId,
                    keyType: 'private_key'
                });
            }

            const wallet = walletsResult.wallets[0];
            const accounts = await client.getWalletAccounts({
                organizationId: subOrgId,
                walletId: wallet.walletId
            });
            const address = accounts.accounts?.[0]?.address || null;
            return sendJSON(res, 200, {
                address,
                signWith: address,
                keyType: 'wallet'
            });
        } catch (err) {
            console.error('Turnkey wallet info error:', err.message);
            return sendJSON(res, 500, { error: err.message });
        }
    }

    // POST /turnkey/export-wallet-account — export and decrypt private key for a wallet account
    if (req.method === 'POST' && (path === '/turnkey/export-wallet-account' || path === '/turnkey/export-private-key')) {
        try {
            const { subOrgId, address } = await readBody(req);
            if (!subOrgId || !address) {
                return sendJSON(res, 400, { error: 'subOrgId and address required' });
            }

            const targetKeyPair = generateP256KeyPair();
            let exportResult = null;
            let lastExportError = null;

            try {
                exportResult = await client.exportWalletAccount({
                    organizationId: subOrgId,
                    address,
                    targetPublicKey: targetKeyPair.publicKey
                });
            } catch (err) {
                lastExportError = err;
            }

            // Fallback: some deployments only support uncompressed public keys.
            if (!exportResult?.exportBundle) {
                try {
                    exportResult = await client.exportWalletAccount({
                        organizationId: subOrgId,
                        address,
                        targetPublicKey: targetKeyPair.publicKeyUncompressed
                    });
                } catch (err) {
                    lastExportError = err;
                }
            }

            const exportBundle = exportResult?.exportBundle;
            if (!exportBundle) {
                throw new Error(lastExportError?.message || 'Turnkey export did not return exportBundle');
            }

            const privateKeyHex = await decryptExportBundle({
                exportBundle,
                embeddedKey: targetKeyPair.privateKey,
                organizationId: subOrgId
            });

            if (!privateKeyHex || privateKeyHex.length !== 64) {
                throw new Error('Invalid exported private key format');
            }

            return sendJSON(res, 200, {
                success: true,
                privateKey: '0x' + privateKeyHex
            });
        } catch (err) {
            console.error('Turnkey export-wallet-account error:', err.message);
            return sendJSON(res, 500, { error: err.message });
        }
    }

    return sendJSON(res, 404, { error: 'Turnkey route not found' });
}

const server = http.createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Access-Control-Allow-Origin', '127.0.0.1');

    if (req.method === 'GET' && req.url === '/health') {
        res.writeHead(200);
        res.end(JSON.stringify({ status: 'ok', ready: !!signClient, turnkey: !!turnkey }));
        return;
    }

    if (req.url.startsWith('/turnkey/')) {
        return handleTurnkey(req, res, req.url);
    }

    if (req.method === 'GET' && req.url === '/uri') {
        if (!signClient) {
            res.writeHead(503);
            res.end(JSON.stringify({ error: 'WalletConnect not ready yet. Please retry.' }));
            return;
        }

        signClient.connect({
            requiredNamespaces: {},
            optionalNamespaces: {
                eip155: {
                    methods: ['eth_accounts', 'eth_sendTransaction', 'personal_sign'],
                    chains: ['eip155:42220', 'eip155:1'],
                    events: ['chainChanged', 'accountsChanged']
                }
            }
        }).then(({ uri, approval }) => {
            const id = Math.random().toString(36).slice(2) + Date.now().toString(36);
            sessions[id] = { uri, status: 'pending', address: null, created: Date.now() };

            approval().then(wcSession => {
                let address = null;
                const ns = wcSession.namespaces || {};
                for (const key of Object.keys(ns)) {
                    const accts = ns[key].accounts || [];
                    if (accts.length) {
                        address = accts[0].split(':').pop();
                        break;
                    }
                }
                if (address) {
                    sessions[id].address = address;
                    sessions[id].topic = wcSession.topic;
                    sessions[id].status = 'approved';
                } else {
                    sessions[id].status = 'rejected';
                }
            }).catch(() => {
                sessions[id].status = 'rejected';
            });

            res.writeHead(200);
            res.end(JSON.stringify({ id, uri }));
        }).catch(err => {
            res.writeHead(500);
            res.end(JSON.stringify({ error: err.message || 'Failed to create WalletConnect session' }));
        });
        return;
    }

    if (req.method === 'GET' && req.url.startsWith('/session/')) {
        const id = req.url.split('/session/')[1];
        const s = sessions[id];
        if (!s) {
            res.writeHead(404);
            res.end(JSON.stringify({ error: 'Session not found' }));
        } else {
            res.writeHead(200);
            res.end(JSON.stringify({ status: s.status, address: s.address }));
        }
        return;
    }

    if (req.method === 'POST' && req.url.startsWith('/sign/')) {
        const id = req.url.split('/sign/')[1];
        const s = sessions[id];
        if (!s || s.status !== 'approved' || !s.topic) {
            res.writeHead(400);
            res.end(JSON.stringify({ error: 'Session not approved or topic missing' }));
            return;
        }
        let body = '';
        req.on('data', chunk => { body += chunk; });
        req.on('end', async () => {
            try {
                const { message, address } = JSON.parse(body);
                const msgHex = '0x' + Buffer.from(message, 'utf8').toString('hex');
                const signature = await signClient.request({
                    topic: s.topic,
                    chainId: 'eip155:42220',
                    request: { method: 'personal_sign', params: [msgHex, address] }
                });
                res.writeHead(200);
                res.end(JSON.stringify({ signature }));
            } catch (err) {
                res.writeHead(200);
                res.end(JSON.stringify({ error: err.message || 'Signing failed or cancelled' }));
            }
        });
        return;
    }

    if (req.method === 'POST' && req.url.startsWith('/tx/')) {
        const id = req.url.split('/tx/')[1];
        const s = sessions[id];
        if (!s || s.status !== 'approved' || !s.topic) {
            res.writeHead(400);
            res.end(JSON.stringify({ error: 'Session not approved or topic missing' }));
            return;
        }
        let body = '';
        req.on('data', chunk => { body += chunk; });
        req.on('end', async () => {
            try {
                const txParams = JSON.parse(body);
                const txHash = await signClient.request({
                    topic: s.topic,
                    chainId: 'eip155:42220',
                    request: { method: 'eth_sendTransaction', params: [txParams] }
                });
                res.writeHead(200);
                res.end(JSON.stringify({ txHash }));
            } catch (err) {
                res.writeHead(200);
                res.end(JSON.stringify({ error: err.message || 'Transaction failed or cancelled' }));
            }
        });
        return;
    }

    res.writeHead(404);
    res.end(JSON.stringify({ error: 'Not found' }));
});

server.on('error', (err) => {
    if (err.code === 'EADDRINUSE') {
        console.error(`WC_ERROR: Port ${PORT} already in use — exiting so supervisor can free it`);
        process.exit(0);
    } else {
        console.error(`WC_ERROR: Server error: ${err.message}`);
        process.exit(1);
    }
});

process.on('unhandledRejection', (reason) => {
    console.error('WC_ERROR: Unhandled rejection:', reason && reason.message ? reason.message : reason);
});

process.on('uncaughtException', (err) => {
    console.error('WC_ERROR: Uncaught exception:', err.message);
});

server.listen(PORT, '127.0.0.1', () => {
    console.log(`WC service listening on port ${PORT}`);
    initClient().catch(err => {
        console.error('WC init failed:', err.message);
    });
});

setInterval(() => {
    const cutoff = Date.now() - 10 * 60 * 1000;
    for (const id of Object.keys(sessions)) {
        if (sessions[id].created < cutoff) delete sessions[id];
    }
}, 10 * 60 * 1000);
