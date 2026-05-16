import sys
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import learn_and_earn.learn_and_earn as learn_module


class FakeBlockchainService:
    is_configured = True
    contract = object()

    def __init__(self, balance):
        self.balance = balance

    async def get_contract_balance(self):
        return self.balance


@pytest.fixture
def app(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(learn_module.learn_earn_bp)
    monkeypatch.setattr(
        learn_module,
        "_get_insufficient_learn_earn_funds_message",
        lambda: "G$ funds have been depleted. Please try again later.",
    )
    return app


def _login(client):
    with client.session_transaction() as sess:
        sess["wallet"] = "0x1234567890abcdef1234567890abcdef12345678"
        sess["verified"] = True


def test_start_quiz_blocks_when_contract_balance_below_1000(app, monkeypatch):
    monkeypatch.setattr(learn_module, "learn_blockchain_service", FakeBlockchainService(999.99))

    client = app.test_client()
    _login(client)

    resp = client.post("/learn-earn/start-quiz", json={})
    data = resp.get_json()

    assert resp.status_code == 400
    assert data["success"] is False
    assert data["blocked"] is True
    assert data["feature_status"] == "insufficient_balance"
    assert data["reason"] == "insufficient_contract_balance"
    assert data["contract_balance"] == pytest.approx(999.99)
    assert data["required_balance"] == pytest.approx(1000)


def test_eligibility_blocks_when_contract_balance_below_1000(app, monkeypatch):
    monkeypatch.setattr(learn_module, "learn_blockchain_service", FakeBlockchainService(250))

    client = app.test_client()
    _login(client)

    resp = client.get("/learn-earn/eligibility")
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["success"] is True
    assert data["eligible"] is False
    assert data["blocked"] is True
    assert data["can_take_now"] is False
    assert data["feature_status"] == "insufficient_balance"
    assert data["contract_balance"] == pytest.approx(250)
    assert data["minimum_contract_balance"] == pytest.approx(1000)
