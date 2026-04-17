import { Turnkey } from '@turnkey/sdk-server';
import { encryptPrivateKeyToBundle } from '@turnkey/crypto';

const TURNKEY_OTP_APP_NAME = process.env.TURNKEY_OTP_APP_NAME || 'GoodMarket';

const TURNKEY_ORG_ID = process.env.TURNKEY_ORGANIZATION_ID;
const TURNKEY_API_PUBLIC_KEY = process.env.TURNKEY_API_PUBLIC_KEY;
const TURNKEY_API_PRIVATE_KEY = process.env.TURNKEY_API_PRIVATE_KEY;

const otpSessionsByEmail = {};

function pruneExpiredOtpSessions() {
  const now = Date.now();
  for (const [email, item] of Object.entries(otpSessionsByEmail)) {
    if (!item || !item.expiresAt || item.expiresAt <= now) {
      delete otpSessionsByEmail[email];
    }
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
    });
    req.on('end', () => {
      try {
        resolve(JSON.parse(body || '{}'));
      } catch {
        reject(new Error('Invalid JSON'));
      }
    });
    req.on('error', reject);
  });
}

function sendJSON(res, statusCode, data) {
  res.statusCode = statusCode;
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify(data));
}

function getTurnkeyClient() {
  if (!TURNKEY_ORG_ID || !TURNKEY_API_PUBLIC_KEY || !TURNKEY_API_PRIVATE_KEY) {
    return { error: 'Turnkey not configured' };
  }

  const turnkey = new Turnkey({
    defaultOrganizationId: TURNKEY_ORG_ID,
    apiBaseUrl: 'https://api.turnkey.com',
    apiPrivateKey: TURNKEY_API_PRIVATE_KEY,
    apiPublicKey: TURNKEY_API_PUBLIC_KEY,
  });

  return { client: turnkey.apiClient() };
}

async function handleTurnkey(req, res, path) {
  const { client, error } = getTurnkeyClient();
  if (error) {
    return sendJSON(res, 503, { error });
  }

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
            curveType: 'API_KEY_CURVE_P256',
          }],
          authenticators: [],
          oauthProviders: [],
        }],
        rootQuorumThreshold: 1,
        wallet: {
          walletName: 'GoodMarket Celo Wallet',
          accounts: [{
            curve: 'CURVE_SECP256K1',
            pathFormat: 'PATH_FORMAT_BIP32',
            path: "m/44'/60'/0'/0/0",
            addressFormat: 'ADDRESS_FORMAT_ETHEREUM',
          }],
        },
      });

      const subOrgId = result.subOrganizationId;
      const walletId = result.wallet?.walletId;
      const accounts = await client.getWalletAccounts({ organizationId: subOrgId, walletId });
      const address = accounts.accounts?.[0]?.address || null;
      return sendJSON(res, 200, { subOrgId, walletId, address });
    } catch (err) {
      return sendJSON(res, 500, { error: err.message });
    }
  }

  if (req.method === 'POST' && path === '/turnkey/import-key') {
    try {
      const { userId, userName, privateKey } = await readBody(req);
      if (!userId || !privateKey) return sendJSON(res, 400, { error: 'userId and privateKey required' });

      const normalizedKey = privateKey.startsWith('0x') ? privateKey.slice(2) : privateKey;

      const subOrgResult = await client.createSubOrganization({
        organizationId: TURNKEY_ORG_ID,
        subOrganizationName: `GoodMarket-${userId}`,
        rootUsers: [{
          userName: userName || userId,
          apiKeys: [{
            apiKeyName: 'GoodMarket Server Key',
            publicKey: TURNKEY_API_PUBLIC_KEY,
            curveType: 'API_KEY_CURVE_P256',
          }],
          authenticators: [],
          oauthProviders: [],
        }],
        rootQuorumThreshold: 1,
      });
      const subOrgId = subOrgResult.subOrganizationId;

      const initResult = await client.initImportPrivateKey({
        organizationId: subOrgId,
        userId: subOrgResult.rootUserIds?.[0],
      });
      const encryptedBundle = await encryptPrivateKeyToBundle({
        privateKeyHex: normalizedKey,
        importBundle: initResult.importBundle,
      });

      const importResult = await client.importPrivateKey({
        organizationId: subOrgId,
        userId: subOrgResult.rootUserIds?.[0],
        keyName: 'GoodMarket Celo Key',
        encryptedBundle,
        curve: 'CURVE_SECP256K1',
        addressFormats: ['ADDRESS_FORMAT_ETHEREUM'],
      });

      return sendJSON(res, 200, {
        subOrgId,
        privateKeyId: importResult.privateKeyId,
        address: importResult.addresses?.[0]?.address || null,
        keyType: 'private_key',
      });
    } catch (err) {
      return sendJSON(res, 500, { error: err.message });
    }
  }

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
        type: 'TRANSACTION_TYPE_ETHEREUM',
      });

      return sendJSON(res, 200, { signedTx: `0x${result.signedTransaction}` });
    } catch (err) {
      return sendJSON(res, 500, { error: err.message });
    }
  }

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
        hashFunction: 'HASH_FUNCTION_KECCAK256',
      });

      const { r, s, v } = result;
      const signature = `0x${r}${s}${(parseInt(v, 16) + 27).toString(16).padStart(2, '0')}`;
      return sendJSON(res, 200, { signature });
    } catch (err) {
      return sendJSON(res, 500, { error: err.message });
    }
  }

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
        expirationSeconds: '600',
      });

      if (!result?.otpId) {
        return sendJSON(res, 500, { error: 'Turnkey did not return otpId' });
      }

      otpSessionsByEmail[normalizedEmail] = {
        otpId: result.otpId,
        expiresAt: Date.now() + 10 * 60 * 1000,
      };

      return sendJSON(res, 200, { success: true, otpId: result.otpId });
    } catch (err) {
      return sendJSON(res, 500, { error: err.message });
    }
  }

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
        otpCode,
      });

      if (!result?.verificationToken) {
        return sendJSON(res, 400, { error: 'Invalid OTP code' });
      }

      delete otpSessionsByEmail[normalizedEmail];
      return sendJSON(res, 200, { success: true });
    } catch (err) {
      return sendJSON(res, 400, { error: err.message || 'Invalid OTP code' });
    }
  }

  if (req.method === 'GET' && path.startsWith('/turnkey/wallet/')) {
    try {
      const subOrgId = path.split('/turnkey/wallet/')[1];
      if (!subOrgId) return sendJSON(res, 400, { error: 'subOrgId required' });

      const walletsResult = await client.getWallets({ organizationId: subOrgId });
      if (!walletsResult.wallets?.length) {
        const keysResult = await client.getPrivateKeys({ organizationId: subOrgId });
        const key = keysResult.privateKeys?.[0];
        return sendJSON(res, 200, {
          address: key?.addresses?.[0]?.address || null,
          signWith: key?.privateKeyId,
          keyType: 'private_key',
        });
      }

      const wallet = walletsResult.wallets[0];
      const accounts = await client.getWalletAccounts({ organizationId: subOrgId, walletId: wallet.walletId });
      return sendJSON(res, 200, {
        address: accounts.accounts?.[0]?.address || null,
        signWith: accounts.accounts?.[0]?.address || null,
        keyType: 'wallet',
      });
    } catch (err) {
      return sendJSON(res, 500, { error: err.message });
    }
  }

  return sendJSON(res, 404, { error: 'Turnkey route not found' });
}

export default async function handler(req, res) {
  const parsed = new URL(req.url, 'http://localhost');
  const path = parsed.pathname.replace(/^\/api\/wc/, '');

  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');

  if (req.method === 'OPTIONS') {
    res.statusCode = 204;
    return res.end();
  }

  if (req.method === 'GET' && path === '/health') {
    return sendJSON(res, 200, { ok: true, service: 'vercel-turnkey-sidecar' });
  }

  if (path.startsWith('/turnkey/')) {
    return handleTurnkey(req, res, path);
  }

  return sendJSON(res, 404, { error: 'Not found' });
}
