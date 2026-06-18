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
TURNKEY_ORG_ID = os.getenv("TURNKEY_ORGANIZATION_ID", "")
TURNKEY_API_PUBLIC_KEY = os.getenv("TURNKEY_API_PUBLIC_KEY", "")
TURNKEY_API_PRIVATE_KEY = os.getenv("TURNKEY_API_PRIVATE_KEY", "")

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
    headers = {
        "Content-Type": "application/json",
        "X-Stamp": stamp,
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
