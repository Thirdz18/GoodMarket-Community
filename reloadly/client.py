import os
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class ReloadlyClient:
    """Reloadly API Client with OAuth2 authentication"""

    SANDBOX_AUTH_URL = "https://auth.reloadly.com"
    PRODUCTION_AUTH_URL = "https://auth.reloadly.com"

    SANDBOX_TOPUP_URL = "https://topups-sandbox.reloadly.com"
    PRODUCTION_TOPUP_URL = "https://topups.reloadly.com"

    SANDBOX_GIFTCARD_URL = "https://giftcards-sandbox.reloadly.com"
    PRODUCTION_GIFTCARD_URL = "https://giftcards.reloadly.com"

    SANDBOX_UTILITY_URL = "https://utilities-sandbox.reloadly.com"
    PRODUCTION_UTILITY_URL = "https://utilities.reloadly.com"

    def __init__(self):
        self.client_id = os.getenv("RELOADLY_CLIENT_ID")
        self.client_secret = os.getenv("RELOADLY_CLIENT_SECRET")
        self.environment = os.getenv("RELOADLY_ENVIRONMENT", "sandbox").lower()

        self.is_sandbox = self.environment == "sandbox"

        self.auth_url = self.SANDBOX_AUTH_URL if self.is_sandbox else self.PRODUCTION_AUTH_URL
        self.topup_url = self.SANDBOX_TOPUP_URL if self.is_sandbox else self.PRODUCTION_TOPUP_URL
        self.giftcard_url = self.SANDBOX_GIFTCARD_URL if self.is_sandbox else self.PRODUCTION_GIFTCARD_URL
        self.utility_url = self.SANDBOX_UTILITY_URL if self.is_sandbox else self.PRODUCTION_UTILITY_URL

        self._topup_token = None
        self._topup_token_expiry = None
        self._giftcard_token = None
        self._giftcard_token_expiry = None
        self._utility_token = None
        self._utility_token_expiry = None

        self.is_initialized = bool(self.client_id and self.client_secret)
        if self.is_initialized:
            logger.info(f"✅ Reloadly client initialized ({self.environment})")
        else:
            logger.warning("⚠️ Reloadly credentials not configured")

    def _get_token(self, audience: str) -> str:
        """Get OAuth2 access token for the given audience"""
        try:
            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
                "audience": audience
            }
            response = requests.post(
                f"{self.auth_url}/oauth/token",
                json=payload,
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            return data.get("access_token")
        except Exception as e:
            logger.error(f"❌ Reloadly auth error for {audience}: {e}")
            raise

    def _get_topup_token(self) -> str:
        now = datetime.utcnow()
        if self._topup_token and self._topup_token_expiry and now < self._topup_token_expiry:
            return self._topup_token
        audience = "https://topups-sandbox.reloadly.com" if self.is_sandbox else "https://topups.reloadly.com"
        self._topup_token = self._get_token(audience)
        self._topup_token_expiry = now + timedelta(hours=1)
        return self._topup_token

    def _get_giftcard_token(self) -> str:
        now = datetime.utcnow()
        if self._giftcard_token and self._giftcard_token_expiry and now < self._giftcard_token_expiry:
            return self._giftcard_token
        audience = "https://giftcards-sandbox.reloadly.com" if self.is_sandbox else "https://giftcards.reloadly.com"
        self._giftcard_token = self._get_token(audience)
        self._giftcard_token_expiry = now + timedelta(hours=1)
        return self._giftcard_token

    def _get_utility_token(self) -> str:
        now = datetime.utcnow()
        if self._utility_token and self._utility_token_expiry and now < self._utility_token_expiry:
            return self._utility_token
        audience = "https://utilities-sandbox.reloadly.com" if self.is_sandbox else "https://utilities.reloadly.com"
        self._utility_token = self._get_token(audience)
        self._utility_token_expiry = now + timedelta(hours=1)
        return self._utility_token

    def _topup_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_topup_token()}",
            "Content-Type": "application/json",
            "Accept": "application/com.reloadly.topups-v1+json"
        }

    def _giftcard_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_giftcard_token()}",
            "Content-Type": "application/json",
            "Accept": "application/com.reloadly.giftcards-v1+json"
        }

    def _utility_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_utility_token()}",
            "Content-Type": "application/json",
            "Accept": "application/com.reloadly.utilities-v1+json"
        }

    # ─── TOP-UP ────────────────────────────────────────────────────────────────

    def get_topup_operators(self, country_code: str) -> list:
        """Get mobile operators for a country"""
        try:
            url = f"{self.topup_url}/operators/countries/{country_code}"
            resp = requests.get(url, headers=self._topup_headers(), timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ get_topup_operators error: {e}")
            raise

    def get_operator_products(self, operator_id: int) -> dict:
        """Get products/denominations for a specific operator"""
        try:
            url = f"{self.topup_url}/operators/{operator_id}"
            resp = requests.get(url, headers=self._topup_headers(), timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ get_operator_products error: {e}")
            raise

    def auto_detect_operator(self, phone_number: str, country_code: str) -> dict:
        """Auto-detect the operator for a phone number"""
        try:
            url = f"{self.topup_url}/operators/auto-detect/phone/{phone_number}/countries/{country_code}"
            params = {"suggestedAmountsMap": "true", "suggestedAmounts": "true"}
            resp = requests.get(url, headers=self._topup_headers(), params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ auto_detect_operator error: {e}")
            raise

    def send_topup(self, operator_id: int, amount: float, phone_number: str, country_code: str, custom_identifier: str = None) -> dict:
        """Send a mobile top-up"""
        try:
            payload = {
                "operatorId": operator_id,
                "amount": amount,
                "useLocalAmount": False,
                "customIdentifier": custom_identifier or f"order_{datetime.utcnow().timestamp()}",
                "recipientPhone": {
                    "countryCode": country_code,
                    "number": phone_number
                }
            }
            resp = requests.post(
                f"{self.topup_url}/topups",
                json=payload,
                headers=self._topup_headers(),
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ send_topup error: {e}")
            raise

    def get_topup_transaction(self, transaction_id: int) -> dict:
        """Get top-up transaction status"""
        try:
            url = f"{self.topup_url}/topups/reports/transactions/{transaction_id}"
            resp = requests.get(url, headers=self._topup_headers(), timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ get_topup_transaction error: {e}")
            raise

    # ─── DATA BUNDLES ──────────────────────────────────────────────────────────

    def get_data_operators(self, country_code: str) -> list:
        """Get data bundle operators for a country"""
        try:
            url = f"{self.topup_url}/operators/countries/{country_code}"
            params = {"bundlesOnly": "true"}
            resp = requests.get(url, headers=self._topup_headers(), params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ get_data_operators error: {e}")
            raise

    # ─── GIFT CARDS ────────────────────────────────────────────────────────────

    def get_giftcard_products(self, country_code: str = None, page: int = 1, size: int = 20) -> dict:
        """Get available gift card products"""
        try:
            url = f"{self.giftcard_url}/products"
            params = {"size": size, "page": page}
            if country_code:
                params["countryCode"] = country_code
            resp = requests.get(url, headers=self._giftcard_headers(), params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ get_giftcard_products error: {e}")
            raise

    def get_giftcard_product(self, product_id: int) -> dict:
        """Get a specific gift card product"""
        try:
            url = f"{self.giftcard_url}/products/{product_id}"
            resp = requests.get(url, headers=self._giftcard_headers(), timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ get_giftcard_product error: {e}")
            raise

    def order_giftcard(self, product_id: int, quantity: int, unit_price: float, custom_identifier: str = None) -> dict:
        """Order a gift card"""
        try:
            payload = {
                "productId": product_id,
                "quantity": quantity,
                "unitPrice": unit_price,
                "customIdentifier": custom_identifier or f"gc_{datetime.utcnow().timestamp()}"
            }
            resp = requests.post(
                f"{self.giftcard_url}/orders",
                json=payload,
                headers=self._giftcard_headers(),
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ order_giftcard error: {e}")
            raise

    def get_giftcard_order(self, order_id: int) -> dict:
        """Get gift card order status"""
        try:
            url = f"{self.giftcard_url}/orders/transactions/{order_id}"
            resp = requests.get(url, headers=self._giftcard_headers(), timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ get_giftcard_order error: {e}")
            raise

    # ─── UTILITY PAYMENTS ──────────────────────────────────────────────────────

    def get_utility_billers(self, country_code: str = None) -> list:
        """Get utility billers"""
        try:
            url = f"{self.utility_url}/billers"
            params = {}
            if country_code:
                params["countryISOCode"] = country_code
            resp = requests.get(url, headers=self._utility_headers(), params=params, timeout=15)
            resp.raise_for_status()
            return resp.json().get("content", [])
        except Exception as e:
            logger.error(f"❌ get_utility_billers error: {e}")
            raise

    def pay_utility(self, biller_id: int, amount: float, subscriber_id: str, custom_identifier: str = None) -> dict:
        """Pay a utility bill"""
        try:
            payload = {
                "billerId": biller_id,
                "amount": amount,
                "subscriberId": subscriber_id,
                "customIdentifier": custom_identifier or f"util_{datetime.utcnow().timestamp()}"
            }
            resp = requests.post(
                f"{self.utility_url}/billers/{biller_id}/pay",
                json=payload,
                headers=self._utility_headers(),
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ pay_utility error: {e}")
            raise

    def get_countries(self) -> list:
        """Get list of supported countries for top-ups"""
        try:
            url = f"{self.topup_url}/countries"
            resp = requests.get(url, headers=self._topup_headers(), timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ get_countries error: {e}")
            raise


reloadly_client = ReloadlyClient()
