# Privy Migration Proposal - GoodMarket

## Executive Summary

**Goal:** Replace current fragmented wallet integrations (WalletConnect custom bridge + direct window.ethereum) with Privy's unified auth SDK.

**Current State:**
- ✅ `@privy-io/react-auth` already in package.json (but NOT used)
- ✅ 369 lines of custom `wc-bridge.js` code to maintain
- ✅ Multiple session tracking variables (`login_method`, `wallet_address`, etc.)

**Target State:**
- Single SDK for all auth methods
- Email + Google + All wallets in ONE modal
- ~50 lines of auth code instead of 369+
- Automatic embedded wallet creation for non-crypto users

---

## Comparison: Current vs Privy

| Aspect | Current Setup | Privy After Migration |
|--------|--------------|----------------------|
| **Bundle Size** | ~600KB (wc-bundle.js) | ~150KB (Privy SDK) |
| **Code to Maintain** | 369+ lines custom JS | ~50 lines config |
| **Email Login** | ❌ None | ✅ Magic Link |
| **Google Login** | ❌ None | ✅ OAuth |
| **MetaMask** | ✅ Direct window.ethereum | ✅ Built-in |
| **WalletConnect** | ✅ Custom bridge | ✅ Built-in |
| **Coinbase Wallet** | ❌ None | ✅ Built-in |
| **Embedded Wallet** | ❌ None | ✅ Auto-created |
| **Session Management** | Multiple cookies/vars | Single `user` object |
| **Chain Support** | Manual config | Built-in Celo support |

---

## Migration Phases

### Phase 1: Backend Auth Simplification
**Estimated Time:** 2-3 hours

#### 1.1 Update `/verify-identity` endpoint
Replace current signature verification with Privy JWT verification:

```python
@routes.route('/verify-identity', methods=['POST'])
def verify_identity():
    # Current: Complex signature verification with multiple code paths
    # New: Simple JWT verification from Privy
    
    id_token = request.json.get('id_token')
    privy_user_id = request.json.get('user_id')
    
    # Verify with Privy API
    response = verify_privy_token(id_token)
    if not response.valid:
        return jsonify({'success': False, 'error': 'Invalid token'}), 401
    
    # Get wallet address from Privy
    wallet_address = response.wallet_address
    
    # Create/update session (same as before)
    session['wallet'] = wallet_address
    session['verified'] = True
    session['privy_user_id'] = privy_user_id
    session['login_method'] = 'privy'
    
    return jsonify({'success': True, 'wallet': wallet_address})
```

#### 1.2 Add Privy environment variables
```bash
# .env
PRIVY_APP_ID=your_privy_app_id
PRIVY_APP_SECRET=your_privy_app_secret
```

#### 1.3 Create Privy verification service
```python
# privy_service.py
import httpx
from functools import wraps

PRIVY_APP_ID = os.getenv('PRIVY_APP_ID')
PRIVY_APP_SECRET = os.getenv('PRIVY_APP_SECRET')

def verify_privy_token(id_token: str) -> dict:
    """Verify id_token from Privy and return user info."""
    # Call Privy API to verify token
    # Return wallet address and user metadata
    pass

def create_privy_session(user) -> dict:
    """Create session data from Privy user object."""
    return {
        'wallet': user.wallet_address,
        'verified': True,
        'login_method': 'privy',
        'privy_user_id': user.id,
        'auth_method': user.auth_method,  # 'wallet' | 'google' | 'email'
    }
```

---

### Phase 2: Frontend Integration
**Estimated Time:** 4-6 hours

#### 2.1 Create Privy Auth Provider Component

```jsx
// static/js/privy-auth.jsx (or inline in login.html)
import { PrivyProvider, usePrivy, useWallets } from '@privy-io/react-auth';

function LoginButton() {
  const { login, ready, user } = usePrivy();
  const { wallets } = useWallets();
  
  const wallet = wallets[0];
  const address = wallet?.address;
  
  return (
    <PrivyProvider
      appId={PRIVY_APP_ID}
      config={{
        supportedChains: ['celo', 'celo-alfajores'],
        supportedTokens: {
          'celo': ['0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A'], // G$ token
        },
      }}
    >
      <button onClick={() => login({
        methods: ['wallet', 'email', 'google']
      })}>
        Sign In
      </button>
    </PrivyProvider>
  );
}
```

#### 2.2 Simplified Login Template (login.html)

Replace 369 lines of wallet code with ~30 lines:

```html
<!-- login.html - SIMPLIFIED VERSION -->
<!DOCTYPE html>
<html>
<head>
    <!-- Privy SDK -->
    <script src="https://unpkg.com/@privy-io/react-auth@latest"></script>
</head>
<body>
    <div id="privy-login-container"></div>
    
    <script type="module">
        // Privy Auth Config
        const PRIVY_APP_ID = '{{ privy_app_id }}';
        
        // Initialize Privy
        window.PrivyManager.init({
            appId: PRIVY_APP_ID,
            target: '#privy-login-container',
            
            // Supported login methods
            loginMethods: ['wallet', 'email', 'google'],
            
            // WalletConnect configuration (uses your existing project ID)
            walletConnectProjectId: '{{ walletconnect_project_id }}',
            
            // Chain configuration
            chains: [{
                id: 42220, // Celo Mainnet
                name: 'Celo',
                rpcUrl: 'https://forno.celo.org',
                currency: 'CELO',
                explorer: 'https://celoscan.io'
            }],
            
            // Embedded wallet settings
            embeddedWallets: {
                requireUserLogin: true,
                showWalletUIs: true
            },
            
            // Login callback
            onSuccess: async (user) => {
                // Send to backend for session creation
                const response = await fetch('/api/auth/verify', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        id_token: user.id_token,
                        user_id: user.id
                    })
                });
                
                if (response.ok) {
                    window.location.href = '/dashboard';
                }
            }
        });
    </script>
</body>
</html>
```

#### 2.3 Update Transaction Signing

Replace all `window.ethereum.request()` calls with Privy's unified provider:

```javascript
// BEFORE (complex wallet detection)
const provider = await _vAwaitEthProvider() || 
    (GMWalletConnect.isPreferred() ? await GMWalletConnect.getProvider() : null);

// AFTER (simple Privy provider)
const { wallets } = useWallets();
const wallet = wallets[0];
const provider = wallet.getEthereumProvider();

// All other code stays the same!
const signature = await provider.request({
    method: 'personal_sign',
    params: [message, address]
});
```

---

### Phase 3: Testing & Edge Cases
**Estimated Time:** 3-4 hours

#### 3.1 Test Cases
- [ ] Email magic link login
- [ ] Google OAuth login
- [ ] MetaMask connection via Privy
- [ ] WalletConnect QR code flow
- [ ] Embedded wallet creation
- [ ] Session persistence across page refresh
- [ ] Transaction signing with all methods
- [ ] Chain switching (Celo ↔ XDC)
- [ ] Logout flow for all methods

#### 3.2 Edge Cases to Handle
- [ ] User disconnects wallet externally
- [ ] Session expires (Privy handles this)
- [ ] Network changes during transaction
- [ ] Multiple wallet connections

---

## What Gets Replaced

| Current File | Status | Replacement |
|-------------|--------|-------------|
| `static/js/wc-bridge.js` | DELETE | Privy handles this |
| `static/js/wc-bundle.js` | DELETE | Not needed |
| `login.html` wallet code | REWRITE | ~30 lines of Privy config |
| `main.py` wallet auth | SIMPLIFY | Privy token verification |
| Session variables | SIMPLIFY | Single `user` object |

## What Gets Added

| New File | Purpose |
|----------|---------|
| `privy_service.py` | Backend token verification |
| `PRIVY_APP_ID` env var | Privy configuration |
| `PRIVY_APP_SECRET` env var | Privy secret |

---

## Implementation Checklist

### Prerequisites
- [ ] Get Privy App ID from privy.io
- [ ] Get Privy App Secret
- [ ] Configure WalletConnect Project ID in Privy dashboard
- [ ] Add Celo network in Privy dashboard

### Backend Changes
- [ ] Add `privy_service.py`
- [ ] Add environment variables
- [ ] Update `/verify-identity` endpoint
- [ ] Update session handling
- [ ] Remove old signature verification code

### Frontend Changes
- [ ] Add Privy SDK to login.html
- [ ] Create Privy config
- [ ] Update dashboard transaction signing
- [ ] Update claim page
- [ ] Update all pages using wallets

### Cleanup
- [ ] Remove `static/js/wc-bridge.js`
- [ ] Remove `static/js/wc-bundle.js`
- [ ] Remove WalletConnect custom code
- [ ] Update documentation

---

## Time Estimate

| Phase | Time | Complexity |
|-------|------|------------|
| Phase 1: Backend | 2-3 hours | Medium |
| Phase 2: Frontend | 4-6 hours | Medium-High |
| Phase 3: Testing | 3-4 hours | Low-Medium |
| **Total** | **9-13 hours** | - |

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Privy SDK issues | Test thoroughly with testnet |
| Session compatibility | Maintain backward compat temporarily |
| User wallet changes | Clear migration path |
| Chain support | Privy supports Celo natively |

---

## Success Metrics

After migration:
- ✅ Auth code reduced from 369+ lines to ~50 lines
- ✅ Bundle size reduced by ~450KB
- ✅ Added email + Google login
- ✅ Single SDK to maintain
- ✅ Better error handling via Privy

---

## Questions/Clarifications Needed

1. Do you have a Privy account already?
2. Do you need to maintain backward compatibility with existing users?
3. What's your timeline preference?
4. Do you want me to implement this for you?

---

*Generated: 2026-07-03*
