"""Unit tests for the strict GoodMarket attribution rule.

Covers the ``is_attributable_to_goodmarket`` decision helper used by every
write path (``/fv-callback``, ``mark_verified_via_goodmarket``,
``run_full_backfill``) and by the new ``correct_false_attributions``
admin-endpoint backend.

The helper does an on-chain ``get_identity_expiry`` lookup for the
``last_authenticated`` timestamp; we pass ``on_chain_last_auth`` directly so
these tests stay hermetic (no Celo RPC calls) and run in single-digit ms.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone


class StrictAttributionTests(unittest.TestCase):
    def setUp(self):
        # Force strict mode ON regardless of the host's env. We toggle the
        # module-level constant directly so we can also flip it OFF for the
        # legacy-fallback test below.
        import goodmarket_attribution_backfill as gm_attr
        self._orig_strict_enabled = gm_attr.STRICT_ATTRIBUTION_ENABLED
        gm_attr.STRICT_ATTRIBUTION_ENABLED = True
        self._orig_window = gm_attr.STRICT_ATTRIBUTION_WINDOW_SECONDS
        gm_attr.STRICT_ATTRIBUTION_WINDOW_SECONDS = 30 * 60
        self.gm_attr = gm_attr

    def tearDown(self):
        self.gm_attr.STRICT_ATTRIBUTION_ENABLED = self._orig_strict_enabled
        self.gm_attr.STRICT_ATTRIBUTION_WINDOW_SECONDS = self._orig_window

    # ---- Happy-path: GENUINE GoodMarket-attributed verification -----------

    def test_genuine_attribution_within_window_passes(self):
        """User came to GoodMarket BEFORE verifying AND verified during the
        same GM session (lastAuthenticated within 30 min of face_verified_at).
        This is the only category that should ever flip the flag."""
        first_login = int(datetime(2026, 4, 13, 21, 30, 0, tzinfo=timezone.utc).timestamp())
        last_auth = int(datetime(2026, 4, 13, 21, 45, 24, tzinfo=timezone.utc).timestamp())
        face_verified_at = int(datetime(2026, 4, 13, 21, 55, 11, tzinfo=timezone.utc).timestamp())

        row = {
            "first_login": datetime.fromtimestamp(first_login, tz=timezone.utc).isoformat(),
            "face_verified_at": datetime.fromtimestamp(face_verified_at, tz=timezone.utc).isoformat(),
        }
        decision = self.gm_attr.is_attributable_to_goodmarket(
            "0x9Cb9Aa01180506fd80639439a645b0925c09299b",
            row,
            on_chain_last_auth=last_auth,
        )
        self.assertTrue(decision["attributable"], decision)
        self.assertEqual(decision["reason"], "ok")
        self.assertLess(decision["delta_seconds"], 30 * 60)

    # ---- PRE_VERIFIED: verified BEFORE first GoodMarket login -------------

    def test_pre_verified_before_first_login_rejected(self):
        """User was already on-chain whitelisted before they even visited
        GoodMarket. Must NEVER count as attributed."""
        # Real example from the May-6 audit: 0x2C89A0C8 verified Nov-15-2025,
        # didn't visit GM until Apr-5-2026 (140-day gap).
        last_auth = int(datetime(2025, 11, 15, 9, 49, 27, tzinfo=timezone.utc).timestamp())
        first_login = int(datetime(2026, 4, 5, 1, 32, 39, tzinfo=timezone.utc).timestamp())
        face_verified_at = int(datetime(2026, 4, 5, 1, 32, 42, tzinfo=timezone.utc).timestamp())

        row = {
            "first_login": datetime.fromtimestamp(first_login, tz=timezone.utc).isoformat(),
            "face_verified_at": datetime.fromtimestamp(face_verified_at, tz=timezone.utc).isoformat(),
        }
        decision = self.gm_attr.is_attributable_to_goodmarket(
            "0x2C89A0C859Da108C25ad838179f7a1b8df9B45Fa",
            row,
            on_chain_last_auth=last_auth,
        )
        self.assertFalse(decision["attributable"], decision)
        self.assertEqual(decision["reason"], "verified_before_first_login")

    # ---- POST_VERIFIED_AFTER_LOGIN: verified elsewhere AFTER GM signup ----

    def test_post_verified_outside_session_rejected(self):
        """User registered on GM, then verified weeks later via a DIFFERENT
        dApp, then much later round-tripped back through GM's FV button.
        On-chain lastAuthenticated is far from face_verified_at — strict
        rule must reject."""
        first_login = int(datetime(2026, 1, 22, 4, 10, 0, tzinfo=timezone.utc).timestamp())
        last_auth = int(datetime(2026, 4, 30, 12, 16, 45, tzinfo=timezone.utc).timestamp())
        # FV callback fired 4+ days AFTER lastAuthenticated
        face_verified_at = int(datetime(2026, 5, 4, 14, 40, 4, tzinfo=timezone.utc).timestamp())

        row = {
            "first_login": datetime.fromtimestamp(first_login, tz=timezone.utc).isoformat(),
            "face_verified_at": datetime.fromtimestamp(face_verified_at, tz=timezone.utc).isoformat(),
        }
        decision = self.gm_attr.is_attributable_to_goodmarket(
            "0x20097FC11C4De184eCe4eABC09f5e973dd643597",
            row,
            on_chain_last_auth=last_auth,
        )
        self.assertFalse(decision["attributable"], decision)
        self.assertEqual(decision["reason"], "verification_outside_goodmarket_session")
        self.assertGreater(decision["delta_seconds"], 30 * 60)

    # ---- Edge: never authenticated on-chain -------------------------------

    def test_never_authenticated_rejected(self):
        row = {"first_login": "2026-04-01T00:00:00+00:00",
               "face_verified_at": "2026-04-01T00:05:00+00:00"}
        decision = self.gm_attr.is_attributable_to_goodmarket(
            "0x1111111111111111111111111111111111111111",
            row,
            on_chain_last_auth=0,
        )
        self.assertFalse(decision["attributable"])
        self.assertEqual(decision["reason"], "never_authenticated_on_chain")

    # ---- Edge: missing first_login (null timestamp) -----------------------

    def test_missing_first_login_rejected(self):
        last_auth = int(datetime(2026, 4, 13, 21, 45, 24, tzinfo=timezone.utc).timestamp())
        decision = self.gm_attr.is_attributable_to_goodmarket(
            "0x1111111111111111111111111111111111111111",
            {},  # No first_login, first_seen_unverified, or created_at
            on_chain_last_auth=last_auth,
            reference_unix=last_auth,
        )
        self.assertFalse(decision["attributable"])
        self.assertEqual(decision["reason"], "no_first_login_timestamp")

    # ---- Edge: first_login fallback to created_at -------------------------

    def test_first_login_falls_back_to_created_at(self):
        last_auth = int(datetime(2026, 4, 13, 21, 45, 24, tzinfo=timezone.utc).timestamp())
        face_verified_at = last_auth + 60

        row = {
            "created_at": datetime.fromtimestamp(last_auth - 3600, tz=timezone.utc).isoformat(),
            "face_verified_at": datetime.fromtimestamp(face_verified_at, tz=timezone.utc).isoformat(),
        }
        decision = self.gm_attr.is_attributable_to_goodmarket(
            "0x1111111111111111111111111111111111111111",
            row,
            on_chain_last_auth=last_auth,
        )
        self.assertTrue(decision["attributable"])
        self.assertEqual(decision["reason"], "ok")

    # ---- Edge: window override --------------------------------------------

    def test_custom_window_loosens_rule(self):
        """A bigger window_seconds lets borderline cases through. Useful for
        post-deploy backfills if RPC indexing was lagging that day."""
        first_login = int(datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        last_auth = int(datetime(2026, 4, 1, 0, 5, 0, tzinfo=timezone.utc).timestamp())
        face_verified_at = int(datetime(2026, 4, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp())  # 55 min later

        row = {
            "first_login": datetime.fromtimestamp(first_login, tz=timezone.utc).isoformat(),
            "face_verified_at": datetime.fromtimestamp(face_verified_at, tz=timezone.utc).isoformat(),
        }
        # Default 30-min window: REJECTED.
        d_default = self.gm_attr.is_attributable_to_goodmarket(
            "0x1111111111111111111111111111111111111111",
            row,
            on_chain_last_auth=last_auth,
        )
        self.assertFalse(d_default["attributable"])

        # Custom 2-hour window: ACCEPTED.
        d_loose = self.gm_attr.is_attributable_to_goodmarket(
            "0x1111111111111111111111111111111111111111",
            row,
            on_chain_last_auth=last_auth,
            window_seconds=2 * 3600,
        )
        self.assertTrue(d_loose["attributable"])

    # ---- Legacy-fallback when strict is disabled --------------------------

    def test_strict_disabled_falls_back_to_legacy_check(self):
        """When ``GOODMARKET_ATTRIBUTION_STRICT_ENABLED=0`` the helper must
        fall back to the old "is whitelisted on-chain?" rule."""
        self.gm_attr.STRICT_ATTRIBUTION_ENABLED = False
        try:
            from unittest.mock import patch
            with patch.object(self.gm_attr, "_is_face_verified_on_chain", return_value=True):
                decision = self.gm_attr.is_attributable_to_goodmarket(
                    "0x1111111111111111111111111111111111111111",
                    {},  # no DB row needed in legacy mode
                )
            self.assertTrue(decision["attributable"])
            self.assertEqual(decision["reason"], "strict_disabled_legacy_pass")
        finally:
            self.gm_attr.STRICT_ATTRIBUTION_ENABLED = True


class IsoTimestampParseTests(unittest.TestCase):
    """``_parse_iso_to_unix`` is a tiny helper but the strict rule depends
    on it being lenient about Z vs +00:00, missing tz, ints, etc."""

    def setUp(self):
        from goodmarket_attribution_backfill import _parse_iso_to_unix
        self._parse = _parse_iso_to_unix

    def test_iso_with_z(self):
        expected = int(datetime(2026, 4, 13, 21, 45, 24, tzinfo=timezone.utc).timestamp())
        self.assertEqual(self._parse("2026-04-13T21:45:24Z"), expected)

    def test_iso_with_offset(self):
        expected = int(datetime(2026, 4, 13, 21, 45, 24, tzinfo=timezone.utc).timestamp())
        self.assertEqual(self._parse("2026-04-13T21:45:24+00:00"), expected)

    def test_iso_with_microseconds(self):
        self.assertIsNotNone(self._parse("2026-04-13T21:55:11.719335+00:00"))

    def test_iso_naive_assumed_utc(self):
        expected = int(datetime(2026, 4, 13, 21, 45, 24, tzinfo=timezone.utc).timestamp())
        self.assertEqual(self._parse("2026-04-13T21:45:24"), expected)

    def test_int_passthrough(self):
        self.assertEqual(self._parse(1776462324), 1776462324)

    def test_none_returns_none(self):
        self.assertIsNone(self._parse(None))
        self.assertIsNone(self._parse(""))

    def test_garbage_returns_none(self):
        self.assertIsNone(self._parse("not-a-date"))


if __name__ == "__main__":
    unittest.main()
