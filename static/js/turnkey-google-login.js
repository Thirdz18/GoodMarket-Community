/**
 * turnkey-google-login.js
 *
 * Handles the "Create new wallet" flow on the homepage:
 *   1. Email OTP via Turnkey Auth Proxy (direct fetch — no SDK needed)
 *   2. Google social login via Google Identity Services
 *   3. Finalizes the Flask session through the backend
 */
(function () {
  'use strict';

  var _gsiLoaded = false;
  var _gsiLoading = null;
  var _emailOtpId = '';
  var _emailContact = '';

  /* ── Helpers ── */

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
      s.onload = function () { _gsiLoaded = true; resolve(); };
      s.onerror = function () { reject(new Error('Failed to load Google Identity Services SDK')); };
      document.head.appendChild(s);
    });
    return _gsiLoading;
  }

  function statusEl(kind) {
    return document.getElementById(kind === 'email' ? 'turnkeyEmailStatus' : 'turnkeyGoogleStatus');
  }

  function showStatus(kind, msg, type) {
    var el = statusEl(kind);
    if (!el) return;
    el.className = 'status-message ' + (type || 'info');
    el.textContent = msg;
    el.style.display = msg ? 'block' : 'none';
  }

  function showOtpWrap(show) {
    var el = document.getElementById('turnkeyEmailOtpWrap');
    if (el) el.style.display = show ? 'block' : 'none';
  }

  function showVerifySection(show) {
    var el = document.getElementById('turnkeyVerifySection');
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
    var el = document.getElementById('googleReferralCode');
    return el ? el.value.trim() : '';
  }

  /* ── Turnkey Auth Proxy: direct fetch (no SDK) ── */

  function _proxyPost(subpath, body) {
    var headers = { 'Content-Type': 'application/json' };
    var configId = window.__TURNKEY_AUTH_PROXY_CONFIG_ID || '';
    if (configId) headers['X-Auth-Proxy-Config-ID'] = configId;
    return fetch('/api/turnkey/auth-proxy/' + subpath, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(body),
    }).then(function (resp) {
      var contentType = resp.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        // Non-JSON response (HTML error page, wrong server, etc.)
        throw new Error('Server error (' + resp.status + '). Please try again or use Google sign-in.');
      }
      return resp.json().then(function (data) {
        if (!resp.ok) {
          var msg = data.error || data.message || ('Request failed (' + resp.status + ')');
          // Translate Turnkey "Not Found" into something actionable
          if (msg === 'Not Found' || resp.status === 404) {
            msg = 'Email login is unavailable right now. Please use Google sign-in instead.';
          }
          throw new Error(msg);
        }
        return data;
      });
    });
  }

  /* ── Session finalization ── */

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
      return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
    }).then(function (result) {
      if (!result.ok || !result.data.success) {
        throw new Error(result.data.error || 'Session finalization failed');
      }
      var warning = result.data.referral_warning;
      showStatus(kind,
        warning ? ('✅ Wallet ready! ⚠️ ' + warning + ' Redirecting…') : '✅ Wallet ready! Redirecting…',
        warning ? 'warning' : 'success'
      );
      setTimeout(function () { window.location.href = '/wallet'; }, warning ? 2400 : 900);
    }).catch(function (err) {
      showStatus(kind, '❌ ' + (err.message || 'Could not finalize wallet'), 'error');
    });
  }

  /* ── Email OTP flow (uses /api/turnkey/email-otp-* — no auth proxy needed) ── */

  function sendEmailOtp() {
    var email = getEmailValue();
    if (!email) {
      showStatus('email', 'Please enter an email address first.', 'warning');
      return Promise.resolve(false);
    }

    showStatus('email', '⏳ Sending a code to your email…', 'info');
    showVerifySection(false);

    return fetch('/api/turnkey/email-otp-init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email }),
    }).then(function (resp) {
      return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
    }).then(function (result) {
      if (!result.ok || !result.data.success) {
        throw new Error(result.data.error || 'Could not send code');
      }
      _emailContact = email;
      showOtpWrap(true);
      showVerifySection(true);
      showStatus('email', '✅ Code sent! Check your email (and spam folder).', 'success');
      var codeInput = document.getElementById('turnkeyEmailOtpCode');
      if (codeInput) codeInput.focus();
      return true;
    }).catch(function (err) {
      showOtpWrap(false);
      showVerifySection(false);
      var msg = err.message || 'Could not send code';
      if (msg.includes('Failed to fetch') || msg.includes('NetworkError')) {
        msg = 'Network error. Please check your connection and try again.';
      }
      showStatus('email', '❌ ' + msg, 'error');
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

    return fetch('/api/turnkey/email-otp-verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: email,
        otp_code: otpCode,
        user_name: name || '',
        referral_code: getReferralCode(),
      }),
    }).then(function (resp) {
      return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
    }).then(function (result) {
      if (!result.ok || !result.data.success) {
        throw new Error(result.data.error || 'Verification failed');
      }
      var warning = result.data.referral_warning;
      showStatus('email',
        warning ? ('✅ Wallet ready! ⚠️ ' + warning + ' Redirecting…') : '✅ Wallet ready! Redirecting…',
        warning ? 'warning' : 'success'
      );
      setTimeout(function () { window.location.href = '/wallet'; }, warning ? 2400 : 900);
      return true;
    }).catch(function (err) {
      showStatus('email', '❌ ' + (err.message || 'Could not verify code'), 'error');
      return false;
    });
  }

  /* ── Google login flow ── */

  function sendTokenToBackend(idToken, referralCode) {
    showStatus('google', '⏳ Creating your wallet…', 'info');
    return fetch('/api/turnkey/google-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id_token: idToken, referral_code: referralCode || '' }),
    }).then(function (resp) {
      return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
    }).then(function (result) {
      if (!result.ok || !result.data.success) {
        throw new Error(result.data.error || 'Login failed');
      }
      var warning = result.data.referral_warning;
      showStatus('google',
        warning ? ('✅ Wallet ready! ⚠️ ' + warning + ' Redirecting…') : '✅ Wallet ready! Redirecting…',
        warning ? 'warning' : 'success'
      );
      setTimeout(function () { window.location.href = '/wallet'; }, warning ? 2400 : 900);
    }).catch(function (err) {
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

  /* ── Email input reset listener ── */

  function setupEmailChangeListener() {
    var emailInput = document.getElementById('turnkeyEmailAddress');
    if (emailInput && !emailInput.dataset.otpResetAttached) {
      emailInput.dataset.otpResetAttached = 'true';
      emailInput.addEventListener('input', function () {
        if (_emailContact && getEmailValue() !== _emailContact) {
          _emailOtpId = '';
          _emailContact = '';
          showOtpWrap(false);
          showVerifySection(false);
          showStatus('email', '', '');
        }
      });
    }
  }

  /* ── Public API ── */

  window.TurnkeyGoogleLogin = { init: initGoogleButton };

  window.TurnkeyAuthFlow = {
    prepare: function () {
      setupEmailChangeListener();
    },
    initGoogleButton: initGoogleButton,
    sendEmailOtp: sendEmailOtp,
    completeEmailOtp: completeEmailOtp,
  };
})();
