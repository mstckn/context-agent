#!/usr/bin/env python3
"""test_ledger.py — Persistent context ledger + decay davranış testleri.

Kabul kriterleri (promt.txt Phase 1):
  * Değişmemiş dosya yeniden GÖNDERİLMEZ (skip).
  * Hash değiştiyse her zaman yeniden gönderilir (content_changed).
  * Temsil seviyesi yetersizse yükseltme yapılır (representation_upgrade).
  * Decay: token biriktikçe FRESH → LIKELY_PRESENT → DECAYING → EXPIRED.
  * Önem sınıfı decay hızını değiştirir (pinned yavaş, ephemeral hızlı).
  * Oturumlar birbirinden izoledir.
  * INSERT kolon hizalaması doğru (first/last_sent_at, turn, sent_session_tokens).
"""

import sys
import tempfile
import unittest
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ledger import ContextLedger, retention_state  # noqa: E402

SCOPE = "test-scope"
WINDOW = 120_000


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "ledger.db"
        self.ledger = ContextLedger(self.db)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.ledger.close)

    def record(self, session="s1", path="a.py", rep="full", h="h1",
               tokens=500, importance="normal", ranges=None):
        return self.ledger.record(SCOPE, session, path, rep, content_hash=h,
                                  token_cost=tokens, importance=importance,
                                  ranges=ranges)

    # ── temel kararlar ──────────────────────────────────────────────

    def test_never_sent_is_send(self):
        st = self.ledger.status(SCOPE, "s1", "unknown.py")
        self.assertEqual(st["decision"], "send")
        self.assertEqual(st["reason"], "never_sent")

    def test_unchanged_file_is_skipped(self):
        self.record(h="h1")
        st = self.ledger.status(SCOPE, "s1", "a.py", new_hash="h1")
        self.assertEqual(st["decision"], "skip")
        self.assertIn("known_to_model", st["reason"])
        self.assertEqual(st["state"], "FRESH")

    def test_changed_file_is_resent(self):
        self.record(h="h1")
        st = self.ledger.status(SCOPE, "s1", "a.py", new_hash="h2")
        self.assertEqual(st["decision"], "send")
        self.assertEqual(st["reason"], "content_changed")

    def test_representation_upgrade(self):
        self.record(rep="outline", h="h1")
        st = self.ledger.status(SCOPE, "s1", "a.py", requested="full",
                                new_hash="h1")
        self.assertEqual(st["decision"], "send")
        self.assertEqual(st["reason"], "representation_upgrade")

    def test_weaker_request_still_skips(self):
        # Model already saw the full file; asking for outline is covered.
        self.record(rep="full", h="h1")
        st = self.ledger.status(SCOPE, "s1", "a.py", requested="outline",
                                new_hash="h1")
        self.assertEqual(st["decision"], "skip")

    # ── decay ───────────────────────────────────────────────────────

    def test_decay_to_decaying_then_expired(self):
        self.record(h="h1")
        # retention = 1 - 80000/120000 ≈ 0.33 → DECAYING → refresh
        self.ledger.advance_turn(SCOPE, "s1", tokens_generated=80_000)
        st = self.ledger.status(SCOPE, "s1", "a.py", new_hash="h1")
        self.assertEqual(st["state"], "DECAYING")
        self.assertEqual(st["decision"], "refresh")

        # retention = 1 - 110000/120000 ≈ 0.08 → EXPIRED → resend
        self.ledger.advance_turn(SCOPE, "s1", tokens_generated=30_000)
        st = self.ledger.status(SCOPE, "s1", "a.py", new_hash="h1")
        self.assertEqual(st["state"], "EXPIRED")
        self.assertEqual(st["decision"], "send")
        self.assertEqual(st["reason"], "expired_from_window")

    def test_pinned_decays_slower_than_ephemeral(self):
        self.record(session="s-pin", path="p.py", importance="pinned")
        self.record(session="s-eph", path="e.py", importance="ephemeral")
        for s in ("s-pin", "s-eph"):
            self.ledger.advance_turn(SCOPE, s, tokens_generated=80_000)
        st_pin = self.ledger.status(SCOPE, "s-pin", "p.py")
        st_eph = self.ledger.status(SCOPE, "s-eph", "e.py")
        # pinned: 1 - 80000*0.35/120000 ≈ 0.77 → LIKELY_PRESENT (skip)
        # ephemeral: 1 - 80000*2/120000 → 0 (clamp) → EXPIRED (send)
        self.assertEqual(st_pin["decision"], "skip")
        self.assertEqual(st_eph["decision"], "send")

    def test_retention_state_thresholds(self):
        self.assertEqual(retention_state(1.0), "FRESH")
        self.assertEqual(retention_state(0.85), "FRESH")
        self.assertEqual(retention_state(0.6), "LIKELY_PRESENT")
        self.assertEqual(retention_state(0.3), "DECAYING")
        self.assertEqual(retention_state(0.1), "EXPIRED")
        self.assertEqual(retention_state(0.0), "EXPIRED")

    def test_resend_resets_decay_clock(self):
        self.record(h="h1")
        self.ledger.advance_turn(SCOPE, "s1", tokens_generated=110_000)
        st = self.ledger.status(SCOPE, "s1", "a.py", new_hash="h1")
        self.assertEqual(st["decision"], "send")
        # Re-record after resend → clock resets → FRESH again.
        self.record(h="h1")
        st = self.ledger.status(SCOPE, "s1", "a.py", new_hash="h1")
        self.assertEqual(st["state"], "FRESH")
        self.assertEqual(st["decision"], "skip")

    # ── oturum izolasyonu ──────────────────────────────────────────

    def test_session_isolation(self):
        self.record(session="s1", h="h1")
        st = self.ledger.status(SCOPE, "s2", "a.py", new_hash="h1")
        self.assertEqual(st["reason"], "never_sent")

    # ── kayıt alanları / regression ─────────────────────────────────

    def test_insert_field_alignment(self):
        # Regression: INSERT dalında first_sent_at/last_sent_at/turn/
        # sent_session_tokens değerleri kayıyordu.
        self.ledger.advance_turn(SCOPE, "s1", tokens_generated=1_000)  # turn=1
        self.record(h="h1")
        known = self.ledger.best_known(SCOPE, "s1", "a.py")
        self.assertIsNotNone(known)
        self.assertEqual(known["turn"], 1)
        self.assertEqual(known["sent_session_tokens"], 1_000)
        self.assertIsInstance(known["last_sent_at"], str)
        self.assertIn("T", known["last_sent_at"])  # ISO timestamp, not int
        self.assertEqual(known["content_hash"], "h1")

    def test_range_merge_on_rerecord(self):
        self.record(rep="range", h="h1", ranges=[[10, 20]])
        self.record(rep="range", h="h1", ranges=[[30, 40]])
        known = self.ledger.best_known(SCOPE, "s1", "a.py")
        self.assertEqual([tuple(r) for r in known["ranges_seen"]],
                         [(10, 20), (30, 40)])
        self.assertEqual(known["token_cost"], 500)  # son kayıt maliyeti

    # ── diff sınıflandırması ────────────────────────────────────────

    def test_diff_buckets(self):
        self.record(path="same.py", h="hs")
        self.record(path="dirty.py", h="hd")
        diff = self.ledger.diff(SCOPE, "s1", [
            {"path": "same.py", "hash": "hs"},
            {"path": "dirty.py", "hash": "hd2"},
            {"path": "fresh.py", "hash": "hf"},
        ])
        self.assertEqual([e["path"] for e in diff["unchanged"]], ["same.py"])
        self.assertEqual([e["path"] for e in diff["changed"]], ["dirty.py"])
        self.assertEqual([e["path"] for e in diff["new"]], ["fresh.py"])

    # ── bakım ───────────────────────────────────────────────────────

    def test_forget_and_list(self):
        self.record(h="h1")
        self.assertEqual(len(self.ledger.known_files(SCOPE, "s1")), 1)
        self.ledger.forget(SCOPE, "s1", "a.py")
        self.assertEqual(len(self.ledger.known_files(SCOPE, "s1")), 0)
        st = self.ledger.status(SCOPE, "s1", "a.py")
        self.assertEqual(st["reason"], "never_sent")

    def test_full_forget_resets_session_counters(self):
        self.record(h="h1")
        self.ledger.set_last_package_tokens(SCOPE, "s1", 5_000)
        self.ledger.advance_turn(SCOPE, "s1", tokens_generated=5_000)
        state = self.ledger.session_state(SCOPE, "s1")
        self.assertGreater(state["turn"], 0)
        self.assertGreater(state["total_tokens"], 0)

        self.ledger.forget(SCOPE, "s1")
        state = self.ledger.session_state(SCOPE, "s1")
        self.assertEqual(state["turn"], 0)
        self.assertEqual(state["total_tokens"], 0)
        self.assertEqual(state["last_package_tokens"], 0)

    def test_concurrent_session_init_and_range_merge(self):
        barrier = threading.Barrier(2)
        errors = []

        def writer(rng):
            ledger = ContextLedger(self.db)
            try:
                barrier.wait()
                ledger.record(SCOPE, "shared", "a.py", "range",
                              content_hash="h1", ranges=[rng])
            except Exception as exc:  # collected for the parent assertion
                errors.append(exc)
            finally:
                ledger.close()

        threads = [threading.Thread(target=writer, args=([10, 20],)),
                   threading.Thread(target=writer, args=([30, 40],))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        known = self.ledger.best_known(SCOPE, "shared", "a.py")
        self.assertEqual([tuple(r) for r in known["ranges_seen"]],
                         [(10, 20), (30, 40)])


if __name__ == "__main__":
    unittest.main()
