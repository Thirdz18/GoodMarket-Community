import json
import os
import unittest
from unittest.mock import patch

from flask import Flask

from routes import _support_desk_ticket_endpoint, routes


class SupportTicketTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret"
        self.app.register_blueprint(routes)
        self.client = self.app.test_client()

    def test_support_endpoint_accepts_base_url(self):
        self.assertEqual(
            _support_desk_ticket_endpoint("https://support.example.com"),
            "https://support.example.com/api/goodmarket/support-tickets",
        )

    def test_support_endpoint_accepts_full_endpoint_url(self):
        self.assertEqual(
            _support_desk_ticket_endpoint("https://support.example.com/api/goodmarket/support-tickets/"),
            "https://support.example.com/api/goodmarket/support-tickets",
        )

    @patch.dict(
        os.environ,
        {
            "SUPPORT_DESK_BASE_URL": "https://support.example.com/api/goodmarket/support-tickets/",
            "GOODMARKET_SUPPORT_API_SECRET": "test-secret-token",
        },
    )
    @patch("routes.urllib.request.urlopen")
    def test_guest_ticket_uses_full_endpoint_and_guest_email_id(self, mock_urlopen):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"ok": True, "ticketCode": "SUP-1", "status": "open"}).encode("utf-8")

        mock_urlopen.return_value = _Resp()

        response = self.client.post(
            "/api/support-tickets",
            json={
                "category": "Other",
                "message": "Please help with my account.",
                "user": {"name": "Guest User", "email": "Guest@Example.COM"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

        outbound_request = mock_urlopen.call_args.args[0]
        self.assertEqual(
            outbound_request.full_url,
            "https://support.example.com/api/goodmarket/support-tickets",
        )
        payload = json.loads(outbound_request.data.decode("utf-8"))
        self.assertEqual(payload["externalUserId"], "guest:guest@example.com")
        self.assertEqual(payload["externalUserEmail"], "Guest@Example.COM")


if __name__ == "__main__":
    unittest.main()
