import unittest
from unittest.mock import patch

from flask import Flask

from routes import routes


class FakeWeb3:
    class HTTPProvider:
        def __init__(self, *_args, **_kwargs):
            pass

    def __init__(self, _provider):
        self.provider = _provider

    @staticmethod
    def to_checksum_address(addr):
        return addr


class FaucetFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret"
        self.app.register_blueprint(routes)
        self.client = self.app.test_client()

    def _auth_session(self, wallet="0x1111111111111111111111111111111111111111"):
        with self.client.session_transaction() as sess:
            sess["verified"] = True
            sess["wallet"] = wallet

    @patch("routes.Web3", new=FakeWeb3)
    @patch("routes._has_recent_refill")
    @patch("routes._get_gas_status")
    def test_case1_enough_celo_no_topup(self, mock_get_gas, mock_recent):
        self._auth_session()
        mock_recent.return_value = (False, 0)
        mock_get_gas.return_value = {
            "balance_wei": "2000000000000000",
            "balance_celo": 0.002,
            "estimated_gas": 220000,
            "gas_price_wei": "1000000000",
            "required_gas_wei": "1000000000000000",
            "required_gas_celo": 0.001,
            "gas_ready": True,
        }

        resp = self.client.post("/api/faucet/gas", json={"wallet": "0x1111111111111111111111111111111111111111"})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["gas_ready"])
        self.assertFalse(body["attempted_api"])
        self.assertFalse(body["attempted_onchain"])
        self.assertEqual(body["terminal_status"], "gas_ready")

    @patch("routes.urllib.request.urlopen")
    @patch("routes.Web3", new=FakeWeb3)
    @patch("routes._poll_balance_increase")
    @patch("routes._has_recent_refill")
    @patch("routes._get_gas_status")
    def test_case2_zero_celo_api_success(self, mock_get_gas, mock_recent, mock_poll, mock_urlopen):
        self._auth_session()
        mock_recent.return_value = (False, 0)
        mock_get_gas.side_effect = [
            {
                "balance_wei": "0",
                "balance_celo": 0.0,
                "estimated_gas": 220000,
                "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000",
                "required_gas_celo": 0.001,
                "gas_ready": False,
            },
            {
                "balance_wei": "2000000000000000",
                "balance_celo": 0.002,
                "estimated_gas": 220000,
                "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000",
                "required_gas_celo": 0.001,
                "gas_ready": True,
            },
        ]
        mock_poll.return_value = (2000000000000000, True)

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"ok":1,"txHash":"0xapi"}'
        mock_urlopen.return_value = _Resp()

        resp = self.client.post("/api/faucet/gas", json={"wallet": "0x1111111111111111111111111111111111111111"})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["topup_source"], "api")
        self.assertTrue(body["attempted_api"])
        self.assertFalse(body["attempted_onchain"])
        self.assertEqual(body["terminal_status"], "gas_ready")

    @patch("routes._execute_onchain_faucet_topup")
    @patch("routes.urllib.request.urlopen")
    @patch("routes.Web3", new=FakeWeb3)
    @patch("routes._get_gas_status")
    @patch("routes._has_recent_refill")
    def test_case3_api_fail_onchain_uses_games_key_path(
        self, mock_recent, mock_get_gas, mock_urlopen, mock_onchain
    ):
        self._auth_session()
        mock_recent.return_value = (False, 0)
        mock_get_gas.side_effect = [
            {
                "balance_wei": "0", "balance_celo": 0.0, "estimated_gas": 220000, "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000", "required_gas_celo": 0.001, "gas_ready": False,
            },
            {
                "balance_wei": "1200000000000000", "balance_celo": 0.0012, "estimated_gas": 220000, "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000", "required_gas_celo": 0.001, "gas_ready": True,
            },
        ]
        mock_urlopen.side_effect = Exception("api down")
        mock_onchain.return_value = {"success": True, "status": "onchain_sent", "tx_hash": "0xonchain"}

        resp = self.client.post("/api/faucet/gas", json={"wallet": "0x1111111111111111111111111111111111111111"})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["attempted_onchain"])
        self.assertEqual(body["topup_source"], "onchain")
        self.assertTrue(mock_onchain.called)

    @patch("routes.Web3", new=FakeWeb3)
    @patch("routes._has_recent_refill")
    @patch("routes._get_gas_status")
    def test_case4_recent_refill_branch(self, mock_get_gas, mock_recent):
        self._auth_session()
        mock_recent.return_value = (True, 42)
        mock_get_gas.return_value = {
            "balance_wei": "0", "balance_celo": 0.0, "estimated_gas": 220000, "gas_price_wei": "1000000000",
            "required_gas_wei": "1000000000000000", "required_gas_celo": 0.001, "gas_ready": False,
        }

        resp = self.client.post("/api/faucet/gas", json={"wallet": "0x1111111111111111111111111111111111111111"})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["terminal_status"], "recent_refill")
        self.assertFalse(body["attempted_api"])
        self.assertFalse(body["attempted_onchain"])

    @patch("routes.os.getenv")
    @patch("routes.Web3", new=FakeWeb3)
    @patch("routes._has_recent_refill")
    @patch("routes._get_gas_status")
    @patch("routes.urllib.request.urlopen")
    def test_case5_missing_games_key_not_configured(
        self, mock_urlopen, mock_get_gas, mock_recent, mock_getenv
    ):
        self._auth_session()
        mock_recent.return_value = (False, 0)
        mock_get_gas.side_effect = [
            {
                "balance_wei": "0", "balance_celo": 0.0, "estimated_gas": 220000, "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000", "required_gas_celo": 0.001, "gas_ready": False,
            },
            {
                "balance_wei": "0", "balance_celo": 0.0, "estimated_gas": 220000, "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000", "required_gas_celo": 0.001, "gas_ready": False,
            },
        ]
        mock_urlopen.side_effect = Exception("api down")
        mock_getenv.side_effect = lambda k, d=None: "" if k == "GAMES_KEY" else d

        resp = self.client.post("/api/faucet/gas", json={"wallet": "0x1111111111111111111111111111111111111111"})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["terminal_status"], "not_configured")
        self.assertTrue(body["attempted_onchain"])
        self.assertEqual(body["onchain_result"]["reason"], "not_configured")

    @patch("routes._execute_onchain_faucet_topup")
    @patch("routes.Web3", new=FakeWeb3)
    @patch("routes._has_recent_refill")
    @patch("routes._get_gas_status")
    def test_case6_force_onchain_bypasses_recent_refill_with_retries(
        self, mock_get_gas, mock_recent, mock_onchain
    ):
        self._auth_session()
        mock_recent.return_value = (True, 120)
        mock_get_gas.side_effect = [
            {
                "balance_wei": "0", "balance_celo": 0.0, "estimated_gas": 220000, "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000", "required_gas_celo": 0.001, "gas_ready": False,
            },
            {
                "balance_wei": "1500000000000000", "balance_celo": 0.0015, "estimated_gas": 220000, "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000", "required_gas_celo": 0.001, "gas_ready": True,
            },
        ]
        mock_onchain.side_effect = [
            {"success": False, "status": "onchain_failed", "error": "temporary fail"},
            {"success": True, "status": "onchain_sent", "tx_hash": "0xretryok"},
        ]

        resp = self.client.post(
            "/api/faucet/gas",
            json={"wallet": "0x1111111111111111111111111111111111111111", "force_onchain": True},
        )
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["attempted_onchain"])
        self.assertFalse(body["attempted_api"])
        self.assertEqual(body["topup_source"], "onchain")
        self.assertTrue(body["gas_ready"])
        self.assertEqual(body["debug"]["fallback_reason"], "forced_onchain")
        self.assertEqual(body["onchain_attempts"], 2)
        self.assertEqual(len(body["onchain_attempt_history"]), 2)
        self.assertEqual(mock_onchain.call_count, 2)

    @patch("routes.urllib.request.urlopen")
    @patch("routes.Web3", new=FakeWeb3)
    @patch("routes._has_recent_refill")
    @patch("routes._get_gas_status")
    def test_case7_force_onchain_string_false_does_not_force(
        self, mock_get_gas, mock_recent, mock_urlopen
    ):
        self._auth_session()
        mock_recent.return_value = (False, 0)
        mock_get_gas.side_effect = [
            {
                "balance_wei": "0",
                "balance_celo": 0.0,
                "estimated_gas": 220000,
                "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000",
                "required_gas_celo": 0.001,
                "gas_ready": False,
            },
            {
                "balance_wei": "2000000000000000",
                "balance_celo": 0.002,
                "estimated_gas": 220000,
                "gas_price_wei": "1000000000",
                "required_gas_wei": "1000000000000000",
                "required_gas_celo": 0.001,
                "gas_ready": True,
            },
        ]

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"ok":1,"txHash":"0xapi"}'
        mock_urlopen.return_value = _Resp()

        resp = self.client.post(
            "/api/faucet/gas",
            json={"wallet": "0x1111111111111111111111111111111111111111", "force_onchain": "false"},
        )
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["attempted_api"])
        self.assertFalse(body["attempted_onchain"])
        self.assertEqual(body["topup_source"], "api")
        self.assertFalse(body["debug"]["force_onchain"])


if __name__ == "__main__":
    unittest.main()
