"""
Privy Authentication Service for GoodMarket
==========================================
Handles all Privy-related authentication operations including:
- Token verification
- User creation/update
- Wallet address extraction
- Session creation

Author: AI Assistant
Date: 2026-07-03
"""

import os
import logging
import time
from typing import Optional
from functools import wraps
from datetime import datetime, timezone

logger = logging.getLogger("privy_service")

# Privy Configuration
PRIVY_APP_ID = os.getenv("PRIVY_APP_ID", "")
PRIVY_APP_SECRET = os.getenv("PRIVY_APP_SECRET", "")
PRIVY_API_BASE = "https://api.privy.io/v1"

# Cache for access token
_access_token_cache = {"token": None, "expires_at": 0}
_access_token_lock = __import__("threading").Lock()


def _get_privy_access_token() -> Optional[str]:
    """
    Get a fresh Privy API access token using app credentials.
    Uses caching to avoid excessive token requests.
    """
    global _access_token_cache
    
    # Check cache first
    with _access_token_lock:
        if _access_token_cache["token"] and _access_token_cache["expires_at"] > time.time():
            return _access_token_cache["token"]
    
    if not PRIVY_APP_ID or not PRIVY_APP_SECRET:
        logger.warning("⚠️ Privy credentials not configured")
        return None
    
    try:
        import requests
        
        response = requests.post(
            f"{PRIVY_API_BASE}/app/token",
            json={
                "app_id": PRIVY_APP_ID,
                "app_secret": PRIVY_APP_SECRET,
            },
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        
        if response.status_code == 200:
            data = response.json()
            token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            
            with _access_token_lock:
                _access_token_cache["token"] = token
                _access_token_cache["expires_at"] = time.time() + expires_in - 60  # Refresh 1 min early
            
            logger.info("✅ Privy access token obtained")
            return token
        else:
            logger.error(f"❌ Failed to get Privy token: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error getting Privy access token: {e}")
        return None


def verify_privy_id_token(id_token: str) -> dict:
    """
    Verify a Privy ID token and return user information.
    
    Args:
        id_token: The JWT ID token from Privy login
        
    Returns:
        dict with keys:
            - valid: bool
            - error: str (if invalid)
            - user_id: str
            - wallet_address: str (if wallet login)
            - auth_method: str ('wallet', 'google', 'email', 'discord', etc.)
            - linked_accounts: list of linked account info
    """
    if not id_token:
        return {"valid": False, "error": "No ID token provided"}
    
    access_token = _get_privy_access_token()
    if not access_token:
        return {"valid": False, "error": "Privy API not configured"}
    
    try:
        import requests
        
        response = requests.get(
            f"{PRIVY_API_BASE}/auth/verify",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Privy-App-ID": PRIVY_APP_ID,
            },
            json={"id_token": id_token},
            timeout=10,
        )
        
        if response.status_code == 200:
            data = response.json()
            user = data.get("user", {})
            
            # Extract wallet address from linked accounts
            wallet_address = None
            auth_method = None
            linked_accounts = user.get("linked_accounts", [])
            
            for account in linked_accounts:
                account_type = account.get("type")
                
                if account_type == "wallet":
                    # Get the wallet address
                    wallet_data = account.get("wallet", {})
                    wallet_address = wallet_data.get("address", "").lower()
                    auth_method = "wallet"
                    
                elif account_type == "google_oauth":
                    auth_method = "google"
                    
                elif account_type == "email":
                    auth_method = "email"
            
            return {
                "valid": True,
                "user_id": user.get("id"),
                "wallet_address": wallet_address,
                "auth_method": auth_method,
                "linked_accounts": linked_accounts,
                "created_at": user.get("created_at"),
                "has_wallet": wallet_address is not None,
            }
            
        elif response.status_code == 401:
            # Token expired or invalid - clear cache
            with _access_token_lock:
                _access_token_cache["token"] = None
                _access_token_cache["expires_at"] = 0
            return {"valid": False, "error": "Invalid or expired ID token"}
            
        else:
            logger.error(f"❌ Privy verify error: {response.status_code} - {response.text}")
            return {"valid": False, "error": f"Verification failed: {response.status_code}"}
            
    except Exception as e:
        logger.error(f"❌ Error verifying Privy token: {e}")
        return {"valid": False, "error": str(e)}


def get_user_from_privy_id(privy_user_id: str) -> Optional[dict]:
    """
    Get user information from Privy by user ID.
    
    Args:
        privy_user_id: The Privy user ID
        
    Returns:
        dict with user info or None if not found
    """
    access_token = _get_privy_access_token()
    if not access_token:
        return None
    
    try:
        import requests
        
        response = requests.get(
            f"{PRIVY_API_BASE}/users/{privy_user_id}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Privy-App-ID": PRIVY_APP_ID,
            },
            timeout=10,
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.warning(f"⚠️ Failed to get Privy user: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error getting Privy user: {e}")
        return None


def create_session_data(privy_result: dict, wallet_address: str = None) -> dict:
    """
    Create Flask session data from Privy verification result.
    
    Args:
        privy_result: Result from verify_privy_id_token()
        wallet_address: Override wallet address if needed
        
    Returns:
        dict with session-ready data
    """
    # Use provided wallet or extract from Privy result
    wallet = wallet_address or privy_result.get("wallet_address")
    
    session_data = {
        "wallet": wallet,
        "wallet_address": wallet,
        "verified": True,
        "ubi_verified": True,
        "login_method": "privy",
        "auth_method": privy_result.get("auth_method", "wallet"),
        "privy_user_id": privy_result.get("user_id"),
        "verification_time": datetime.now(timezone.utc).isoformat(),
        "is_new_user": False,  # Will be determined by caller
    }
    
    return session_data


def is_privy_configured() -> bool:
    """Check if Privy is properly configured."""
    return bool(PRIVY_APP_ID and PRIVY_APP_SECRET)


# Export for convenience
__all__ = [
    "verify_privy_id_token",
    "get_user_from_privy_id", 
    "create_session_data",
    "is_privy_configured",
]
