from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from backend.app.refresh_worker import _auto_refresh_enabled, _close_verification_due, _market_session, _next_auto_refresh_at


class AutoRefreshScheduleTest(unittest.TestCase):
    def test_auto_refresh_is_always_enabled(self) -> None:
        samples = [
            datetime(2026, 6, 22, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 22, 17, 0, tzinfo=UTC),
            datetime(2026, 6, 21, 4, 0, tzinfo=UTC),
        ]

        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(_auto_refresh_enabled(sample))
                self.assertEqual(_market_session(sample), "always_on")

    def test_next_auto_refresh_uses_interval_at_all_times(self) -> None:
        now = datetime(2026, 6, 21, 4, 0, tzinfo=UTC)

        self.assertEqual(_next_auto_refresh_at(now, 60), now + timedelta(seconds=60))


class CloseVerificationScheduleTest(unittest.TestCase):
    @patch("backend.app.refresh_worker._last_close_verification_at", return_value=None)
    def test_close_verification_waits_until_1800_taipei(self, _last_refresh) -> None:
        self.assertFalse(_close_verification_due(datetime(2026, 6, 22, 9, 59, tzinfo=UTC)))
        self.assertTrue(_close_verification_due(datetime(2026, 6, 22, 10, 0, tzinfo=UTC)))

    @patch("backend.app.refresh_worker._last_close_verification_at")
    def test_pre_1800_same_day_run_does_not_block_final_pass(self, last_refresh) -> None:
        last_refresh.return_value = datetime(2026, 6, 22, 6, 5, tzinfo=UTC)

        self.assertTrue(_close_verification_due(datetime(2026, 6, 22, 10, 0, tzinfo=UTC)))

    @patch("backend.app.refresh_worker._last_close_verification_at")
    def test_post_1800_same_day_run_is_not_repeated(self, last_refresh) -> None:
        last_refresh.return_value = datetime(2026, 6, 22, 10, 5, tzinfo=UTC)

        self.assertFalse(_close_verification_due(datetime(2026, 6, 22, 11, 0, tzinfo=UTC)))


if __name__ == "__main__":
    unittest.main()
