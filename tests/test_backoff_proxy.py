"""Pending retry backoff + proxy-down skip (no network)."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_td = tempfile.TemporaryDirectory()
os.environ["AI_KB_ROOT"] = _td.name  # before importing kb modules

from kb import cli, state  # noqa: E402
from kb.sources import youtube  # noqa: E402

T0 = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)


def _seen():
    (Path(_td.name) / "state").mkdir(exist_ok=True)
    s = state.Seen()
    s.data = {}
    s.save = lambda: None
    return s


class BackoffTests(unittest.TestCase):
    def test_schedule(self):
        h = lambda n: state.backoff_delay(n)
        self.assertEqual([h(1), h(2), h(3), h(4), h(9)],
                         [timedelta(hours=x) for x in (1, 6, 24, 48, 48)])

    def test_fail_attempt_sets_next_try_and_skips(self):
        s = _seen()
        s.mark("c", "v", "pending", save=False, first_seen=T0.isoformat())
        s.data["c"]["v"]["first_seen"] = T0.isoformat()
        e = s.fail_attempt("c", "v", "no subtitles", now=T0)
        self.assertEqual(e["attempts"], 1)
        self.assertEqual(state.parse_iso(e["next_try"]), T0 + timedelta(hours=1))
        self.assertFalse(s.should_process("c", "v", now=T0 + timedelta(minutes=30)))
        self.assertTrue(s.should_process("c", "v", now=T0 + timedelta(hours=1, seconds=1)))
        s.force_pending = True
        self.assertTrue(s.should_process("c", "v", now=T0 + timedelta(minutes=1)))

    def test_missing_fields_eligible_now(self):
        s = _seen()
        s.data = {"c": {"v": {"status": "pending", "first_seen": T0.isoformat(), "attempts": 103}}}
        # legacy entry with 103 attempts is over the cap -> given up, not retried
        self.assertFalse(s.should_process("c", "v", now=T0))
        self.assertEqual(s.data["c"]["v"]["status"], "failed")
        s.data = {"c": {"w": {"status": "pending", "first_seen": T0.isoformat()}}}
        self.assertTrue(s.should_process("c", "w", now=T0))

    def test_gives_up_after_max_attempts_and_age(self):
        s = _seen()
        s.data = {"c": {"v": {"status": "pending", "first_seen": T0.isoformat(),
                              "attempts": state.MAX_ATTEMPTS - 1}}}
        e = s.fail_attempt("c", "v", "x", now=T0 + timedelta(days=1))
        self.assertEqual(e["status"], "failed")
        self.assertIn("gave up", e["last_error"])
        s.data = {"c": {"v": {"status": "pending", "first_seen": T0.isoformat(), "attempts": 2}}}
        e = s.fail_attempt("c", "v", "x", now=T0 + timedelta(days=31))
        self.assertEqual(e["status"], "failed")

    def test_unattempted_queue_not_expired_at_30d(self):
        s = _seen()
        s.data = {"c": {"v": {"status": "pending", "first_seen": T0.isoformat(), "attempts": 0}}}
        self.assertTrue(s.should_process("c", "v", now=T0 + timedelta(days=40)))


class ProxyDownTests(unittest.TestCase):
    def setUp(self):
        self.run = cli.Run()
        self.cfg = {"key": "c", "type": "youtube"}

    def test_proxy_down_skips_without_attempts_and_counts(self):
        seen = _seen()
        seen.data = {"c": {"v": {"status": "pending", "first_seen": T0.isoformat(), "attempts": 1}}}
        with mock.patch.object(youtube, "youtube_reachable", return_value=False) as chk, \
                mock.patch.object(cli, "process_video") as pv, \
                mock.patch.object(youtube, "discover_rss", return_value=[]), \
                self.assertLogs("kb", level="WARNING") as lg:
            for _ in range(2):  # two sources in the same run -> one check, one warning
                cli.sync_youtube(self.cfg, seen, None, self.run, False, pending_only=True)
        self.assertEqual(chk.call_count, 1)
        pv.assert_not_called()
        self.assertEqual(sum("proxy_down" in m for m in lg.output), 1)
        self.assertEqual(seen.data["c"]["v"]["attempts"], 1)

    def test_consecutive_counter_and_banner(self):
        for _ in range(3):
            n = state.record_proxy(False)
        self.assertEqual(n, 3)
        from kb import index
        self.assertIn("proxy down", index._proxy_banner())
        self.assertEqual(state.record_proxy(True), 0)
        self.assertIsNone(index._proxy_banner())


if __name__ == "__main__":
    unittest.main()
