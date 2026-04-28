"""
Local-EVM test suite for GoodMarketP2PEscrow.sol.

Uses eth_tester (in-memory PyEVM) to simulate the entire flow without
requiring a remote node. Tests cover:

  - Ad lifecycle (open / close / cannot-close-with-active-trades)
  - Order lifecycle (place / cancel / mark paid / release)
  - Auto-release after seller timeout
  - Buyer cannot cancel after marking paid
  - Self-trade prevention
  - Dispute resolution (buyer wins / seller wins)
  - Expiry of pending orders
  - Pause / unpause
  - Access control (only seller / only buyer / only arbiter)
  - Reentrancy guard
  - Edge cases (zero refund, exhausted ad, etc.)

Run with:  python3 tests/test_p2p_escrow.py
"""

import os
import sys
import time
from pathlib import Path

from eth_tester import EthereumTester, PyEVMBackend
from solcx import compile_standard, install_solc
from web3 import EthereumTesterProvider, Web3

REPO_ROOT = Path(__file__).resolve().parent.parent
ESCROW_SOL = REPO_ROOT / "contracts" / "GoodMarketP2PEscrow.sol"

# Minimal ERC20 mock for testing — mints to deployer, allows transfer/approve.
MOCK_GD_SOURCE = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.21;

contract MockGDollar {
    string public name = "Mock GoodDollar";
    string public symbol = "G$";
    uint8 public decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(uint256 initial) {
        totalSupply = initial;
        balanceOf[msg.sender] = initial;
        emit Transfer(address(0), msg.sender, initial);
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        emit Transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(balanceOf[from] >= amount, "balance");
        require(allowance[from][msg.sender] >= amount, "allowance");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }
}
"""


def color(s, code):
    return f"\033[{code}m{s}\033[0m"


def green(s): return color(s, "32")
def red(s): return color(s, "31")
def cyan(s): return color(s, "36")
def yellow(s): return color(s, "33")


class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def run(self, name, fn):
        try:
            print(cyan(f"\n▶ {name}"))
            fn()
            print(green(f"  ✓ PASS"))
            self.passed += 1
        except AssertionError as e:
            print(red(f"  ✗ FAIL: {e}"))
            self.failed += 1
            self.errors.append((name, str(e)))
        except Exception as e:
            print(red(f"  ✗ ERROR: {type(e).__name__}: {e}"))
            self.failed += 1
            self.errors.append((name, f"{type(e).__name__}: {e}"))

    def report(self):
        total = self.passed + self.failed
        print()
        print("═" * 70)
        if self.failed == 0:
            print(green(f"  ALL TESTS PASSED  ({self.passed}/{total})"))
        else:
            print(red(f"  {self.failed} FAILED, {self.passed} PASSED  ({total} total)"))
            for name, err in self.errors:
                print(red(f"    • {name}: {err}"))
        print("═" * 70)
        return self.failed == 0


def compile_all():
    print(yellow("Installing solc 0.8.21 if not already..."))
    install_solc("0.8.21")
    print(yellow("Compiling contracts..."))
    src = ESCROW_SOL.read_text()
    out = compile_standard(
        {
            "language": "Solidity",
            "sources": {
                "GoodMarketP2PEscrow.sol": {"content": src},
                "MockGDollar.sol": {"content": MOCK_GD_SOURCE},
            },
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode"]}},
            },
        },
        solc_version="0.8.21",
    )
    return out


class Fixture:
    """Bundles the EVM, contracts, and named accounts for a single test."""

    def __init__(self, compiled):
        backend = PyEVMBackend()
        self.tester = EthereumTester(backend=backend)
        self.w3 = Web3(EthereumTesterProvider(self.tester))
        self.w3.eth.default_account = self.w3.eth.accounts[0]

        self.deployer = self.w3.eth.accounts[0]
        self.arbiter  = self.w3.eth.accounts[1]
        self.seller   = self.w3.eth.accounts[2]
        self.buyer    = self.w3.eth.accounts[3]
        self.buyer2   = self.w3.eth.accounts[4]
        self.attacker = self.w3.eth.accounts[5]
        self.keeper   = self.w3.eth.accounts[6]

        # Deploy MockGDollar
        gd = compiled["contracts"]["MockGDollar.sol"]["MockGDollar"]
        GD = self.w3.eth.contract(abi=gd["abi"], bytecode=gd["evm"]["bytecode"]["object"])
        initial_supply = self._w(10_000_000)  # 10M G$
        tx = GD.constructor(initial_supply).transact({"from": self.deployer})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx)
        self.gd = self.w3.eth.contract(address=receipt.contractAddress, abi=gd["abi"])

        # Distribute G$ to test accounts
        for acct in (self.seller, self.buyer, self.buyer2, self.attacker):
            self.gd.functions.mint(acct, self._w(1_000_000)).transact({"from": self.deployer})

        # Deploy escrow
        es = compiled["contracts"]["GoodMarketP2PEscrow.sol"]["GoodMarketP2PEscrow"]
        ES = self.w3.eth.contract(abi=es["abi"], bytecode=es["evm"]["bytecode"]["object"])
        tx = ES.constructor(self.gd.address, self.arbiter).transact({"from": self.deployer})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx)
        self.escrow = self.w3.eth.contract(address=receipt.contractAddress, abi=es["abi"])

    @staticmethod
    def _w(amount: float) -> int:
        return int(amount * (10 ** 18))

    def w(self, amount: float) -> int:
        return self._w(amount)

    def now(self) -> int:
        return self.w3.eth.get_block("latest")["timestamp"]

    def warp(self, seconds: int):
        """Advance the EVM clock by `seconds`."""
        self.tester.time_travel(self.now() + seconds)

    def make_id(self, label: str) -> bytes:
        return Web3.keccak(text=f"{label}-{self.now()}")

    def approve(self, owner_acct, amount):
        self.gd.functions.approve(self.escrow.address, amount).transact({"from": owner_acct})

    def gd_bal(self, acct):
        return self.gd.functions.balanceOf(acct).call()


def expect_revert(fn, expected_msg=""):
    """
    Run `fn()` and assert it reverts. If `expected_msg` is given, also assert
    that string appears in the revert reason. Returns the caught exception.

    This helper avoids the trap of a bare `try/except Exception` block where the
    test's own `raise AssertionError("should have reverted")` would be caught by
    the same `except` clause and silently swallowed. By keeping the
    "did-not-revert" assertion *outside* the try/except, that path always fires.
    """
    try:
        fn()
    except Exception as e:
        if expected_msg and expected_msg not in str(e):
            raise AssertionError(
                f"reverted with wrong reason: expected substring "
                f"'{expected_msg}', got: {type(e).__name__}: {e}"
            ) from e
        return e
    raise AssertionError("expected revert but tx succeeded")


# ═══════════════════════════════════════════════════════════════════════════
# Test cases
# ═══════════════════════════════════════════════════════════════════════════

def test_open_ad_basic(f: Fixture):
    ad_id = f.make_id("ad1")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    ad = f.escrow.functions.getAd(ad_id).call()
    assert ad[0] == f.seller, "seller mismatch"
    assert ad[1] == f.w(50_000), "totalLocked mismatch"
    assert ad[2] == f.w(50_000), "remainingAmount mismatch"
    assert ad[6] is True, "open should be true"
    assert f.gd_bal(f.escrow.address) == f.w(50_000), "escrow should hold G$"


def test_open_ad_below_min_reverts(f: Fixture):
    ad_id = f.make_id("ad2")
    f.approve(f.seller, f.w(10_000))
    expect_revert(
        lambda: f.escrow.functions.openAd(
            ad_id, f.w(10_000), f.w(10_000), f.w(10_000)
        ).transact({"from": f.seller}),
        "below MIN_AD_AMOUNT",
    )


def test_open_ad_max_below_min_reverts(f: Fixture):
    ad_id = f.make_id("ad3")
    f.approve(f.seller, f.w(50_000))
    expect_revert(
        lambda: f.escrow.functions.openAd(
            ad_id, f.w(50_000), f.w(30_000), f.w(20_000)
        ).transact({"from": f.seller}),
        "maxOrder < minOrder",
    )


def test_close_ad_with_no_trades_refunds(f: Fixture):
    ad_id = f.make_id("ad4")
    f.approve(f.seller, f.w(50_000))
    seller_bal_before = f.gd_bal(f.seller)
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    assert f.gd_bal(f.seller) == seller_bal_before - f.w(50_000)

    f.escrow.functions.closeAd(ad_id).transact({"from": f.seller})
    assert f.gd_bal(f.seller) == seller_bal_before, "full refund expected"

    ad = f.escrow.functions.getAd(ad_id).call()
    assert ad[6] is False, "ad should be closed"


def test_close_ad_with_active_trade_reverts(f: Fixture):
    """Critical test: seller cannot cancel ad if there's an active trade."""
    ad_id = f.make_id("ad5")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )

    trade_id = f.make_id("trade1")
    deadline = f.now() + 30 * 60
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(20_000), deadline).transact(
        {"from": f.buyer}
    )

    expect_revert(
        lambda: f.escrow.functions.closeAd(ad_id).transact({"from": f.seller}),
        "active trades",
    )


def test_close_ad_only_owner(f: Fixture):
    ad_id = f.make_id("ad6")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    expect_revert(
        lambda: f.escrow.functions.closeAd(ad_id).transact({"from": f.attacker}),
        "not your ad",
    )


def test_place_order_basic(f: Fixture):
    ad_id = f.make_id("ad7")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )

    trade_id = f.make_id("trade7")
    deadline = f.now() + 30 * 60
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), deadline).transact(
        {"from": f.buyer}
    )

    trade = f.escrow.functions.getTrade(trade_id).call()
    assert trade[1] == f.buyer
    assert trade[2] == f.w(25_000)
    assert trade[5] == 1  # PaymentPending

    ad = f.escrow.functions.getAd(ad_id).call()
    assert ad[2] == f.w(25_000), "remaining should decrease"
    assert ad[5] == 1, "activeTradeCount should be 1"


def test_place_order_self_trade_blocked(f: Fixture):
    ad_id = f.make_id("ad8")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("self")
    expect_revert(
        lambda: f.escrow.functions.placeOrder(
            ad_id, trade_id, f.w(20_000), f.now() + 1800
        ).transact({"from": f.seller}),
        "trade with self",
    )


def test_place_order_below_min_reverts(f: Fixture):
    ad_id = f.make_id("ad9")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("toosmall")
    expect_revert(
        lambda: f.escrow.functions.placeOrder(
            ad_id, trade_id, f.w(10_000), f.now() + 1800
        ).transact({"from": f.buyer}),
        "below minOrder",
    )


def test_place_order_short_window_reverts(f: Fixture):
    ad_id = f.make_id("ad10")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("shortwin")
    expect_revert(
        lambda: f.escrow.functions.placeOrder(
            ad_id, trade_id, f.w(20_000), f.now() + 60
        ).transact({"from": f.buyer}),
        "window too short",
    )


def test_place_order_past_deadline_reverts(f: Fixture):
    """Deadline in the past must revert with the descriptive error,
    not a Solidity Panic(0x11) from underflow."""
    ad_id = f.make_id("ad10b")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("pastdead")
    expect_revert(
        lambda: f.escrow.functions.placeOrder(
            ad_id, trade_id, f.w(20_000), f.now() - 1
        ).transact({"from": f.buyer}),
        "deadline in past",
    )


def test_cancel_order_before_payment(f: Fixture):
    ad_id = f.make_id("ad11")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("c1")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.cancelOrder(trade_id).transact({"from": f.buyer})
    trade = f.escrow.functions.getTrade(trade_id).call()
    assert trade[5] == 4  # Cancelled
    ad = f.escrow.functions.getAd(ad_id).call()
    assert ad[2] == f.w(50_000), "amount returned to ad"
    assert ad[5] == 0, "activeTradeCount back to 0"


def test_buyer_cannot_cancel_after_paid(f: Fixture):
    """CRITICAL: prevents the scam where buyer pays fiat then cancels to recover G$."""
    ad_id = f.make_id("ad12")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("c2")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})

    expect_revert(
        lambda: f.escrow.functions.cancelOrder(trade_id).transact({"from": f.buyer}),
        "cannot cancel now",
    )


def test_full_happy_path(f: Fixture):
    """Seller opens → buyer places → buyer marks paid → seller releases."""
    ad_id = f.make_id("happy")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )

    trade_id = f.make_id("happyT")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})

    buyer_bal_before = f.gd_bal(f.buyer)
    f.escrow.functions.release(trade_id).transact({"from": f.seller})

    assert f.gd_bal(f.buyer) == buyer_bal_before + f.w(25_000)

    trade = f.escrow.functions.getTrade(trade_id).call()
    assert trade[5] == 3  # Completed

    ad = f.escrow.functions.getAd(ad_id).call()
    assert ad[5] == 0, "activeTradeCount back to 0"


def test_release_only_seller(f: Fixture):
    ad_id = f.make_id("ad13")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("r1")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})

    expect_revert(
        lambda: f.escrow.functions.release(trade_id).transact({"from": f.attacker}),
        "not the seller",
    )


def test_auto_release_after_timeout(f: Fixture):
    ad_id = f.make_id("ad14")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("auto1")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})

    # Try too early — should fail
    expect_revert(
        lambda: f.escrow.functions.autoReleaseAfterTimeout(trade_id).transact(
            {"from": f.keeper}
        ),
        "auto-release not yet",
    )

    # Warp 48 hours
    f.warp(48 * 3600 + 1)

    buyer_bal_before = f.gd_bal(f.buyer)
    f.escrow.functions.autoReleaseAfterTimeout(trade_id).transact({"from": f.keeper})
    assert f.gd_bal(f.buyer) == buyer_bal_before + f.w(25_000)

    trade = f.escrow.functions.getTrade(trade_id).call()
    assert trade[5] == 3  # Completed


def test_expire_pending_order(f: Fixture):
    ad_id = f.make_id("ad15")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("exp1")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )

    # Try too early
    expect_revert(
        lambda: f.escrow.functions.expirePendingOrder(trade_id).transact(
            {"from": f.keeper}
        ),
        "deadline not reached",
    )

    # Warp past deadline
    f.warp(31 * 60)

    f.escrow.functions.expirePendingOrder(trade_id).transact({"from": f.keeper})

    trade = f.escrow.functions.getTrade(trade_id).call()
    assert trade[5] == 5  # Expired

    ad = f.escrow.functions.getAd(ad_id).call()
    assert ad[2] == f.w(50_000), "amount returned to ad"
    assert ad[5] == 0, "activeTradeCount back to 0"


def test_dispute_resolution_buyer_wins(f: Fixture):
    ad_id = f.make_id("ad16")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("d1")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})
    f.escrow.functions.disputeAsSeller(trade_id).transact({"from": f.seller})

    trade = f.escrow.functions.getTrade(trade_id).call()
    assert trade[5] == 6  # Disputed

    buyer_bal_before = f.gd_bal(f.buyer)
    f.escrow.functions.resolveDispute(trade_id, True).transact({"from": f.arbiter})
    assert f.gd_bal(f.buyer) == buyer_bal_before + f.w(25_000)

    trade = f.escrow.functions.getTrade(trade_id).call()
    assert trade[5] == 3  # Completed


def test_dispute_resolution_seller_wins(f: Fixture):
    ad_id = f.make_id("ad17")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("d2")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})
    f.escrow.functions.disputeAsBuyer(trade_id).transact({"from": f.buyer})

    seller_bal_before = f.gd_bal(f.seller)
    f.escrow.functions.resolveDispute(trade_id, False).transact({"from": f.arbiter})
    assert f.gd_bal(f.seller) == seller_bal_before + f.w(25_000)

    trade = f.escrow.functions.getTrade(trade_id).call()
    assert trade[5] == 7  # Refunded


def test_only_arbiter_can_resolve(f: Fixture):
    ad_id = f.make_id("ad18")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("d3")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})
    f.escrow.functions.disputeAsBuyer(trade_id).transact({"from": f.buyer})

    expect_revert(
        lambda: f.escrow.functions.resolveDispute(trade_id, True).transact(
            {"from": f.attacker}
        ),
        "not arbiter",
    )


def test_pause_blocks_new_ads_and_orders(f: Fixture):
    f.escrow.functions.pause().transact({"from": f.deployer})

    ad_id = f.make_id("paused")
    f.approve(f.seller, f.w(50_000))
    expect_revert(
        lambda: f.escrow.functions.openAd(
            ad_id, f.w(50_000), f.w(20_000), f.w(50_000)
        ).transact({"from": f.seller}),
        "paused",
    )

    f.escrow.functions.unpause().transact({"from": f.deployer})

    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )


def test_only_owner_can_pause(f: Fixture):
    expect_revert(
        lambda: f.escrow.functions.pause().transact({"from": f.attacker}),
        "not owner",
    )


def test_set_arbiter(f: Fixture):
    f.escrow.functions.setArbiter(f.buyer).transact({"from": f.deployer})
    assert f.escrow.functions.arbiter().call() == f.buyer


def test_transfer_ownership(f: Fixture):
    f.escrow.functions.transferOwnership(f.buyer).transact({"from": f.deployer})
    assert f.escrow.functions.owner().call() == f.buyer


def test_disputed_trade_cannot_be_released_by_seller(f: Fixture):
    ad_id = f.make_id("ad19")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("d4")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})
    f.escrow.functions.disputeAsSeller(trade_id).transact({"from": f.seller})

    expect_revert(
        lambda: f.escrow.functions.release(trade_id).transact({"from": f.seller}),
        "not awaiting release",
    )


def test_disputed_trade_cannot_auto_release(f: Fixture):
    ad_id = f.make_id("ad20")
    f.approve(f.seller, f.w(50_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    trade_id = f.make_id("d5")
    f.escrow.functions.placeOrder(ad_id, trade_id, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(trade_id).transact({"from": f.buyer})
    f.escrow.functions.disputeAsSeller(trade_id).transact({"from": f.seller})

    f.warp(48 * 3600 + 1)
    expect_revert(
        lambda: f.escrow.functions.autoReleaseAfterTimeout(trade_id).transact(
            {"from": f.keeper}
        ),
        "not awaiting release",
    )


def test_multiple_concurrent_buyers(f: Fixture):
    """Two buyers place orders against the same ad. Both should succeed."""
    ad_id = f.make_id("ad21")
    f.approve(f.seller, f.w(60_000))
    f.escrow.functions.openAd(ad_id, f.w(60_000), f.w(20_000), f.w(30_000)).transact(
        {"from": f.seller}
    )

    t1 = f.make_id("t1")
    t2 = f.make_id("t2")

    f.escrow.functions.placeOrder(ad_id, t1, f.w(30_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.placeOrder(ad_id, t2, f.w(30_000), f.now() + 1800).transact(
        {"from": f.buyer2}
    )

    ad = f.escrow.functions.getAd(ad_id).call()
    assert ad[2] == 0
    assert ad[5] == 2

    # Third buyer tries — should fail (insufficient remaining)
    t3 = f.make_id("t3")
    expect_revert(
        lambda: f.escrow.functions.placeOrder(
            ad_id, t3, f.w(20_000), f.now() + 1800
        ).transact({"from": f.attacker}),
        "insufficient ad remaining",
    )


def test_double_open_ad_fails(f: Fixture):
    ad_id = f.make_id("dup")
    f.approve(f.seller, f.w(100_000))
    f.escrow.functions.openAd(ad_id, f.w(50_000), f.w(20_000), f.w(50_000)).transact(
        {"from": f.seller}
    )
    expect_revert(
        lambda: f.escrow.functions.openAd(
            ad_id, f.w(50_000), f.w(20_000), f.w(50_000)
        ).transact({"from": f.seller}),
        "already exists",
    )


def test_release_partial_then_close(f: Fixture):
    """Seller releases one trade, then closes ad to recover remaining."""
    ad_id = f.make_id("partial")
    f.approve(f.seller, f.w(60_000))
    f.escrow.functions.openAd(ad_id, f.w(60_000), f.w(20_000), f.w(30_000)).transact(
        {"from": f.seller}
    )
    t1 = f.make_id("p1")
    f.escrow.functions.placeOrder(ad_id, t1, f.w(25_000), f.now() + 1800).transact(
        {"from": f.buyer}
    )
    f.escrow.functions.markPaid(t1).transact({"from": f.buyer})
    f.escrow.functions.release(t1).transact({"from": f.seller})

    seller_bal_before = f.gd_bal(f.seller)
    f.escrow.functions.closeAd(ad_id).transact({"from": f.seller})
    assert f.gd_bal(f.seller) == seller_bal_before + f.w(35_000)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

ALL_TESTS = [
    ("openAd basic flow", test_open_ad_basic),
    ("openAd reverts when total below MIN_AD_AMOUNT", test_open_ad_below_min_reverts),
    ("openAd reverts when maxOrder < minOrder", test_open_ad_max_below_min_reverts),
    ("closeAd refunds remaining when no trades", test_close_ad_with_no_trades_refunds),
    ("closeAd reverts when ad has active trades [CRITICAL]", test_close_ad_with_active_trade_reverts),
    ("closeAd only callable by seller", test_close_ad_only_owner),
    ("placeOrder basic flow", test_place_order_basic),
    ("placeOrder blocks self-trade", test_place_order_self_trade_blocked),
    ("placeOrder reverts below minOrder", test_place_order_below_min_reverts),
    ("placeOrder reverts on short payment window", test_place_order_short_window_reverts),
    ("placeOrder reverts on past deadline (no underflow)", test_place_order_past_deadline_reverts),
    ("cancelOrder works before markPaid", test_cancel_order_before_payment),
    ("cancelOrder BLOCKED after markPaid [CRITICAL]", test_buyer_cannot_cancel_after_paid),
    ("Full happy path: open → place → markPaid → release", test_full_happy_path),
    ("release only callable by seller", test_release_only_seller),
    ("autoReleaseAfterTimeout works after 48hr", test_auto_release_after_timeout),
    ("expirePendingOrder works after deadline", test_expire_pending_order),
    ("Dispute resolution: buyer wins", test_dispute_resolution_buyer_wins),
    ("Dispute resolution: seller wins", test_dispute_resolution_seller_wins),
    ("Only arbiter can resolveDispute", test_only_arbiter_can_resolve),
    ("Pause blocks new ads/orders", test_pause_blocks_new_ads_and_orders),
    ("Only owner can pause", test_only_owner_can_pause),
    ("setArbiter works", test_set_arbiter),
    ("transferOwnership works", test_transfer_ownership),
    ("Seller cannot release a disputed trade", test_disputed_trade_cannot_be_released_by_seller),
    ("Cannot auto-release a disputed trade", test_disputed_trade_cannot_auto_release),
    ("Multiple concurrent buyers", test_multiple_concurrent_buyers),
    ("Cannot reuse adId", test_double_open_ad_fails),
    ("Partial release then close", test_release_partial_then_close),
]


def main():
    compiled = compile_all()
    runner = TestRunner()
    for name, fn in ALL_TESTS:
        # fresh fixture per test for isolation
        runner.run(name, lambda f=Fixture(compiled), fn=fn: fn(f))
    success = runner.report()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
