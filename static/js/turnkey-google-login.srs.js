import { OtpType, TurnkeyClient } from '@turnkey/core';

/**
 * turnkey-google-login.js
 *
 * Handles the "Create new wallet" flow on the homepage:
 *   1. Email OTP via Turnkey Auth Proxy
 *   2. Google social login via the existing Google login flow
 *   3. Finalizes the Flask session through the backend
 */
(function () {
  'use strict';

  var _gsiLoaded = false;
  var _gsiLoading = null;
  var _turnkeyClient = null;
  var _emailOtpId = '';
  var _emailContact = '';

  function loadGsi() {
    if (_gsiLoaded && window.google && window.google.accounts) {
      return Promise.resolve();
    }
    if (_gsiLoading) return _gsiLoading;

    _gsiLoading = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = 'https://accounts.google.com/gsi/client';
      s.async = true;
      s.defer = true;
      s.onload = function () {
        _gsiLoaded = true;
        resolve();
      };
      s.onerror = function () {
        reject(new Error('Failed to load Google Identity Services SDK'));
      };
      document.head.appendChild(s);
    });
    return _gsiLoading;
  }

  function statusEl(kind) {
    if (kind === 'email') {
      return document.getElementById('turnkeyEmailStatus');
    }
    return document.getElementById('turnkeyGoogleStatus');
  }

  function showStatus(kind, msg, type) {
    var el = statusEl(kind);
    if (!el) return;
    el.className = 'status-message ' + (type || 'info');
    el.textContent = msg;
  }

  function showOtpWrap(show) {
    var el = document.getElementById('turnkeyEmailOtpWrap');
    if (el) el.style.display = show ? 'block' : 'none';
  }

  function getEmailValue() {
    var el = document.getElementById('turnkeyEmailAddress');
    return el ? el.value.trim() : '';
  }

  function getNameValue() {
    var el = document.getElementById('turnkeyNameInput');
    return el ? el.value.trim() : '';
  }

  function getReferralCode() {
    var refInput = document.getElementById('googleReferralCode');
    return refInput ? refInput.value.trim() : '';
  }

  function getClient() {
    if (_turnkeyClient) {
      return Promise.resolve(_turnkeyClient);
    }

    var orgId = window.__TURNKEY_ORGANIZATION_ID || '';
    var configId = window.__TURNKEY_AUTH_PROXY_CONFIG_ID || '';
    if (!orgId || !configId) {
      return Promise.reject(new Error('Turnkey auth proxy is not configured'));
    }

    _turnkeyClient = new TurnkeyClient({
      organizationId: orgId,
      authProxyConfigId: configId,
      authProxyUrl: '/api/turnkey/auth-proxy',
    });
    return _turnkeyClient.init().then(function () {
      return _turnkeyClient;
    });
  }

  function buildCreateWalletParams(email, name) {
    return {
      apiKeys: [],
      authenticators: [],
      oauthProviders: [],
      subOrgName: name ? ('GoodMarket – ' + name) : ('GoodMarket – ' + email),
      userEmail: email,
      userName: name || email,
      customWallet: {
        walletName: 'Default Wallet',
        walletAccounts: [
          {
            curve: 'CURVE_SECP256K1',
            pathFormat: 'PATH_FORMAT_BIP32',
            path: "m/44'/60'/0'/0/0",
            addressFormat: 'ADDRESS_FORMAT_ETHEREUM',
          },
        ],
      },
    };
  }

  function finalizeSession(kind, sessionToken, loginMethod, email, name) {
    showStatus(kind, '⏳ Finalizing your wallet…', 'info');
    return fetch('/api/turnkey/auth-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_token: sessionToken,
        login_method: loginMethod,
        email: email || '',
        user_name: name || '',
        referral_code: getReferralCode(),
      }),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        return { ok: resp.ok, data: data };
      });
    }).then(function (result) {
      if (!result.ok || !result.data.success) {
        throw new Error(result.data.error || 'Session finalization failed');
      }
      var warning = result.data.referral_warning;
      var okMsg = warning
        ? '✅ Wallet ready! ⚠️ ' + warning + ' Redirecting…'
        : '✅ Wallet ready! Redirecting…';
      showStatus(kind, okMsg, warning ? 'warning' : 'success');
      setTimeout(function () {
        window.location.href = '/wallet';
      }, warning ? 2400 : 900);
    }).catch(function (err) {
      showStatus(kind, '❌ ' + (err.message || 'Could not finalize wallet'), 'error');
    });
  }

  function sendTokenToBackend(idToken, referralCode) {
    showStatus('google', '⏳ Creating your wallet…', 'info');

    return fetch('/api/turnkey/google-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id_token: idToken,
        referral_code: referralCode || ''
      })
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          return { ok: resp.ok, data: data };
        });
      })
      .then(function (result) {
        if (!result.ok || !result.data.success) {
          throw new Error(result.data.error || 'Login failed');
        }
        var warning = result.data.referral_warning;
        var okMsg = warning
          ? '✅ Wallet ready! ⚠️ ' + warning + ' Redirecting…'
          : '✅ Wallet ready! Redirecting…';
        showStatus('google', okMsg, warning ? 'warning' : 'success');
        setTimeout(function () {
          window.location.href = '/wallet';
        }, warning ? 2400 : 900);
      })
      .catch(function (err) {
        showStatus('google', '❌ ' + (err.message || 'Google login failed'), 'error');
      });
  }

  function handleGoogleCredentialResponse(response) {
    if (!response || !response.credential) {
      showStatus('google', '❌ Google sign-in was cancelled', 'warning');
      return;
    }
    sendTokenToBackend(response.credential, getReferralCode());
  }

  function initGoogleButton() {
    var clientId = window.__GOOGLE_CLIENT_ID;
    if (!clientId) {
      console.warn('[turnkey-google] No GOOGLE_CLIENT_ID configured');
      return;
    }

    loadGsi().then(function () {
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: handleGoogleCredentialResponse,
        auto_select: false,
        cancel_on_tap_outside: true,
      });

      var btnWrap = document.getElementById('googleSignInBtnWrap');
      if (btnWrap) {
        window.google.accounts.id.renderButton(btnWrap, {
          type: 'standard',
          theme: 'filled_blue',
          size: 'large',
          text: 'signin_with',
          shape: 'pill',
          width: 300,
        });
      }
    }).catch(function (err) {
      console.error('[turnkey-google] GIS load error:', err);
      showStatus('google', '❌ Could not load Google Sign-In', 'error');
    });
  }

  function sendEmailOtp() {
    var email = getEmailValue();
    var name = getNameValue();
    if (!email) {
      showStatus('email', 'Please enter an email address first.', 'warning');
      return Promise.resolve(false);
    }

    showStatus('email', '⏳ Sending a code to your email…', 'info');
    return getClient().then(function (client) {
      return client.initOtp({
        otpType: OtpType.Email,
        contact: email,
      }).then(function (otpId) {
        _emailOtpId = otpId;
        _emailContact = email;
        showOtpWrap(true);
        showStatus('email', '✅ Code sent. Enter the 6-digit code to continue.', 'success');
        var codeInput = document.getElementById('turnkeyEmailOtpCode');
        if (codeInput) codeInput.focus();
        return true;
      });
    }).catch(function (err) {
      showStatus('email', '❌ ' + (err.message || 'Could not send code'), 'error');
      return false;
    });
  }

  function completeEmailOtp() {
    var email = getEmailValue();
    var name = getNameValue();
    var codeInput = document.getElementById('turnkeyEmailOtpCode');
    var otpCode = codeInput ? codeInput.value.trim() : '';
    if (!_emailOtpId) {
      showStatus('email', 'Send the email code first.', 'warning');
      return Promise.resolve(false);
    }
    if (!_emailContact || _emailContact !== email) {
      showStatus('email', 'Use the same email address that received the code.', 'warning');
      return Promise.resolve(false);
    }
    if (!otpCode) {
      showStatus('email', 'Enter the code from your email.', 'warning');
      return Promise.resolve(false);
    }

    showStatus('email', '⏳ Verifying your code…', 'info');
    return getClient().then(function (client) {
      return client.completeOtp({
        otpId: _emailOtpId,
        otpCode: otpCode,
        contact: email,
        otpType: OtpType.Email,
        createSubOrgParams: buildCreateWalletParams(email, name),
      }).then(function (result) {
        return finalizeSession('email', result.sessionToken, 'turnkey_email', email, name);
      });
    }).catch(function (err) {
      showStatus('email', '❌ ' + (err.message || 'Could not verify code'), 'error');
      return false;
    });
  }

  function prepare() {
    var email = getEmailValue();
    if (email && !window.__turnkeyEmailOtpVisible) {
      showOtpWrap(false);
    }
  }

  window.TurnkeyGoogleLogin = {
    init: initGoogleButton,
  };

  window.TurnkeyAuthFlow = {
    prepare: prepare,
    initGoogleButton: initGoogleButton,
    sendEmailOtp: sendEmailOtp,
    completeEmailOtp: completeEmailOtp,
  };
})();
