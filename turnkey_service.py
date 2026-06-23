"""Turnkey embedded-wallet service.

Handles sub-organization creation (with Google OIDC) and wallet address
look-up via the Turnkey REST API.
"""

import base64
import json
import logging
import os
import time

import requests as _requests
from turnkey_api_key_stamper import ApiKeyStamper, ApiKeyStamperConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration – all values come from environment variables
# ---------------------------------------------------------------------------
TURNKEY_API_BASE = os.getenv("TURNKEY_API_BASE_URL", "https://api.turnkey.com")
TURNKEY_ORG_ID = os.getenv("TURNKEY_ORGANIZATION_ID", "").strip()

# The env vars may have been copy-pasted incorrectly: the tail of the public key
# can end up prepended (with a space) to the private key value.  Detect and fix.
_raw_pub = os.getenv("TURNKEY_API_PUBLIC_KEY", "").strip()
_raw_priv = os.getenv("TURNKEY_API_PRIVATE_KEY", "").strip()
if " " in _raw_priv:
    _priv_parts = _raw_priv.split(" ", 1)
    # First token is the missing suffix of the public key; second is the real private key.
    _raw_pub = _raw_pub + _priv_parts[0]
    _raw_priv = _priv_parts[1]
TURNKEY_API_PUBLIC_KEY = _raw_pub.replace(" ", "")
TURNKEY_API_PRIVATE_KEY = _raw_priv.replace(" ", "")

# Google OAuth – used to verify the id_token on the backend
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

# Lazy-initialised stamper (avoids import-time crash when env vars are absent)
_stamper = None


def _get_stamper():
    global _stamper
    if _stamper is None:
        if not TURNKEY_API_PUBLIC_KEY or not TURNKEY_API_PRIVATE_KEY:
            raise RuntimeError("TURNKEY_API_PUBLIC_KEY / TURNKEY_API_PRIVATE_KEY not configured")
        cfg = ApiKeyStamperConfig(
            api_public_key=TURNKEY_API_PUBLIC_KEY,
            api_private_key=TURNKEY_API_PRIVATE_KEY,
        )
        _stamper = ApiKeyStamper(cfg)
    return _stamper


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _turnkey_post(path: str, body: dict) -> dict:
    """Send an authenticated POST to the Turnkey API and return the JSON response."""
    body_str = json.dumps(body, separators=(",", ":"))
    stamp = _get_stamper().stamp(body_str)
    stamp_value = stamp.stamp_header_value if hasattr(stamp, 'stamp_header_value') else str(stamp)
    headers = {
        "Content-Type": "application/json",
        "X-Stamp": stamp_value,
    }
    url = f"{TURNKEY_API_BASE}{path}"
    resp = _requests.post(url, data=body_str, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _b64_encode(raw: str) -> str:
    """Standard base64-encode a string (Turnkey expects base64 for OIDC tokens)."""
    return base64.b64encode(raw.encode()).decode()


# ---------------------------------------------------------------------------
# Google ID-token verification
# ---------------------------------------------------------------------------

def verify_google_id_token(id_token: str) -> dict:
    """Verify a Google OIDC id_token using Google's tokeninfo endpoint.

    Returns the decoded token payload (email, sub, etc.) on success.
    Raises ``ValueError`` on any verification failure.
    """
    resp = _requests.get(
        "https://oauth2.googleapis.com/tokeninfo",
        params={"id_token": id_token},
        timeout=10,
    )
    if resp.status_code != 200:
        raise ValueError(f"Google token verification failed: {resp.text}")
    payload = resp.json()
    # Validate audience matches our Google Client ID
    if GOOGLE_CLIENT_ID and payload.get("aud") != GOOGLE_CLIENT_ID:
        raise ValueError("Token audience mismatch")
    if payload.get("email_verified") not in ("true", True):
        raise ValueError("Email not verified by Google")
    return payload


# ---------------------------------------------------------------------------
# Sub-organization + wallet creation
# ---------------------------------------------------------------------------

def create_sub_org_with_wallet(
    google_id_token: str,
    user_email: str,
    user_name: str = "",
) -> dict:
    """Create a Turnkey sub-organization with an Ethereum wallet.

    The Google OIDC token is attached as an OAuth provider on the root user
    so the user can authenticate with Turnkey in the future.

    Returns ``{"sub_org_id": "…", "wallet_id": "…", "wallet_address": "…"}``.
    """
    if not TURNKEY_ORG_ID:
        raise RuntimeError("TURNKEY_ORGANIZATION_ID not configured")

    body = {
        "type": "ACTIVITY_TYPE_CREATE_SUB_ORGANIZATION_V8",
        "timestampMs": str(int(time.time() * 1000)),
        "organizationId": TURNKEY_ORG_ID,
        "parameters": {
            "subOrganizationName": f"GoodMarket – {user_email}",
            "rootUsers": [
                {
                    "userName": user_name or user_email,
                    "userEmail": user_email,
                    "apiKeys": [],
                    "authenticators": [],
                    "oauthProviders": [
                        {
                            "providerName": "Google",
                            "oidcToken": _b64_encode(google_id_token),
                        }
                    ],
                }
            ],
            "rootQuorumThreshold": 1,
            "wallet": {
                "walletName": "Default Wallet",
                "accounts": [
                    {
                        "curve": "CURVE_SECP256K1",
                        "pathFormat": "PATH_FORMAT_BIP32",
                        "path": "m/44'/60'/0'/0/0",
                        "addressFormat": "ADDRESS_FORMAT_ETHEREUM",
                    }
                ],
            },
        },
    }

    data = _turnkey_post("/public/v1/submit/create_sub_organization", body)
    logger.info("Turnkey create_sub_organization response: %s", json.dumps(data)[:300])

    # Navigate the response to extract IDs
    activity = data.get("activity", {})
    result = activity.get("result", {})
    sub_org_result = result.get("createSubOrganizationResultV8") or result.get("createSubOrganizationResult", {})

    sub_org_id = sub_org_result.get("subOrganizationId", "")
    wallet_info = sub_org_result.get("wallet", {})
    wallet_id = wallet_info.get("walletId", "")

    addresses = wallet_info.get("addresses", [])
    wallet_address = addresses[0] if addresses else ""

    if not wallet_address:
        raise RuntimeError(f"Turnkey did not return a wallet address: {json.dumps(sub_org_result)[:200]}")

    return {
        "sub_org_id": sub_org_id,
        "wallet_id": wallet_id,
        "wallet_address": wallet_address,
    }


def get_wallets_for_sub_org(sub_org_id: str) -> list:
    """Retrieve wallets for an existing Turnkey sub-organization."""
    body = {
        "organizationId": sub_org_id,
    }
    data = _turnkey_post("/public/v1/query/list_wallets", body)
    return data.get("wallets", [])


# ---------------------------------------------------------------------------
# Email OTP flow (uses main Turnkey API with server-side stamping)
# ---------------------------------------------------------------------------

def init_email_otp(email: str) -> str:
    """Send a 6-digit OTP to *email* via Turnkey and return the otpId."""
    if not TURNKEY_ORG_ID:
        raise RuntimeError("TURNKEY_ORGANIZATION_ID not configured")

    body = {
        "type": "ACTIVITY_TYPE_INIT_OTP_AUTH",
        "timestampMs": str(int(time.time() * 1000)),
        "organizationId": TURNKEY_ORG_ID,
        "parameters": {
            "otpType": "OTP_TYPE_EMAIL",
            "contact": email,
            "emailCustomization": {
                "appName": "GoodMarket",
            },
        },
    }

    data = _turnkey_post("/public/v1/submit/init_otp_auth", body)
    logger.info("Turnkey init_otp_auth: %s", json.dumps(data)[:300])

    activity = data.get("activity", {})
    status = activity.get("status", "")
    if status not in ("ACTIVITY_STATUS_COMPLETED", "ACTIVITY_STATUS_PENDING"):
        failure = activity.get("failure") or activity
        raise ValueError(f"OTP init failed ({status}): {json.dumps(failure)[:200]}")

    result = activity.get("result", {})
    otp_id = result.get("initOtpAuthResult", {}).get("otpId", "")
    if not otp_id:
        raise ValueError(f"No otpId in Turnkey response: {json.dumps(data)[:200]}")
    return otp_id


def verify_email_otp(otp_id: str, otp_code: str) -> bool:
    """Verify *otp_code* against *otp_id*. Returns True on success, raises on failure."""
    if not TURNKEY_ORG_ID:
        raise RuntimeError("TURNKEY_ORGANIZATION_ID not configured")

    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives import serialization as _ser

    _priv = _ec.generate_private_key(_ec.SECP256R1())
    target_pub_key = _priv.public_key().public_bytes(
        _ser.Encoding.X962,
        _ser.PublicFormat.UncompressedPoint,
    ).hex()

    body = {
        "type": "ACTIVITY_TYPE_OTP_AUTH",
        "timestampMs": str(int(time.time() * 1000)),
        "organizationId": TURNKEY_ORG_ID,
        "parameters": {
            "otpId": otp_id,
            "otpCode": otp_code,
            "targetPublicKey": target_pub_key,
            "apiKeyName": "Email OTP Session",
            "expirationSeconds": "3600",
            "invalidateExisting": True,
        },
    }

    data = _turnkey_post("/public/v1/submit/otp_auth", body)
    logger.info("Turnkey otp_auth: %s", json.dumps(data)[:300])

    activity = data.get("activity", {})
    status = activity.get("status", "")
    if status == "ACTIVITY_STATUS_COMPLETED":
        return True
    failure = activity.get("failure") or activity
    raise ValueError(f"Invalid OTP code ({status}): {json.dumps(failure)[:200]}")


# ---------------------------------------------------------------------------
# Supabase Auth – email OTP (6-digit code sent by Supabase SMTP)
# ---------------------------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()


def send_supabase_otp(email: str) -> None:
    """Ask Supabase Auth to send a 6-digit OTP to *email*.

    Requires the ``SUPABASE_KEY`` (anon/public key) env var.
    Raises ``RuntimeError`` when the key is absent or the call fails.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError(
            "SUPABASE_KEY (anon key) is not configured. "
            "Add it from Supabase → Project Settings → API → anon/public key."
        )
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }
    resp = _requests.post(
        f"{SUPABASE_URL}/auth/v1/otp",
        json={"email": email, "create_user": True},
        headers=headers,
        timeout=15,
    )
    if resp.status_code not in (200, 204):
        body = resp.text[:200]
        raise RuntimeError(f"Supabase OTP send failed ({resp.status_code}): {body}")
    logger.info("Supabase OTP sent to %s", email)


def verify_supabase_otp(email: str, token: str) -> dict:
    """Verify a 6-digit OTP token from Supabase Auth.

    Returns the Supabase user dict on success.
    Raises ``ValueError`` on wrong/expired token, ``RuntimeError`` on other errors.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError("SUPABASE_KEY (anon key) is not configured.")
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }
    resp = _requests.post(
        f"{SUPABASE_URL}/auth/v1/verify",
        json={"type": "email", "token": token, "email": email},
        headers=headers,
        timeout=15,
    )
    if resp.status_code == 401 or resp.status_code == 422:
        raise ValueError("Incorrect or expired code. Please check your email and try again.")
    if not resp.ok:
        body = resp.text[:200]
        raise RuntimeError(f"Supabase OTP verify failed ({resp.status_code}): {body}")
    data = resp.json()
    user = data.get("user") or {}
    logger.info("Supabase OTP verified for %s → user_id=%s", email, user.get("id", "?"))
    return user


def create_sub_org_for_email(email: str, user_name: str = "") -> dict:
    """Create a Turnkey sub-organization for an email-only user (no OAuth provider).

    Returns ``{"sub_org_id": "…", "wallet_id": "…", "wallet_address": "…"}``.
    """
    if not TURNKEY_ORG_ID:
        raise RuntimeError("TURNKEY_ORGANIZATION_ID not configured")

    body = {
        "type": "ACTIVITY_TYPE_CREATE_SUB_ORGANIZATION_V8",
        "timestampMs": str(int(time.time() * 1000)),
        "organizationId": TURNKEY_ORG_ID,
        "parameters": {
            "subOrganizationName": f"GoodMarket-{email}",
            "rootUsers": [
                {
                    "userName": user_name or email,
                    "userEmail": email,
                    "apiKeys": [],
                    "authenticators": [],
                    "oauthProviders": [],
                }
            ],
            "rootQuorumThreshold": 1,
            "wallet": {
                "walletName": "Default Wallet",
                "accounts": [
                    {
                        "curve": "CURVE_SECP256K1",
                        "pathFormat": "PATH_FORMAT_BIP32",
                        "path": "m/44'/60'/0'/0/0",
                        "addressFormat": "ADDRESS_FORMAT_ETHEREUM",
                    }
                ],
            },
        },
    }

    data = _turnkey_post("/public/v1/submit/create_sub_organization", body)
    logger.info("Turnkey create_sub_org_for_email: %s", json.dumps(data)[:300])

    activity = data.get("activity", {})
    result = activity.get("result", {})
    sub_org_result = (
        result.get("createSubOrganizationResultV8")
        or result.get("createSubOrganizationResult", {})
    )

    sub_org_id = sub_org_result.get("subOrganizationId", "")
    wallet_info = sub_org_result.get("wallet", {})
    wallet_id = wallet_info.get("walletId", "")
    addresses = wallet_info.get("addresses", [])
    wallet_address = addresses[0] if addresses else ""

    if not wallet_address:
        raise RuntimeError(
            f"Turnkey did not return a wallet address: {json.dumps(sub_org_result)[:200]}"
        )

    return {
        "sub_org_id": sub_org_id,
        "wallet_id": wallet_id,
        "wallet_address": wallet_address,
    }
