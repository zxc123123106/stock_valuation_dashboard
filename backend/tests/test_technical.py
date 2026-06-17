from __future__ import annotations

import unittest
from decimal import Decimal

from backend.app.technical import moving_averages
from backend.app.finmind_daily import HISTORY_CALENDAR_DAYS


class MovingAverageTest(unittest.TestCase):
    def test_returns_none_when_not_enough_closes(self) -> None:
        values = [Decimal("10"), Decimal("11"), Decimal("12"), Decimal("13")]

        averages = moving_averages(values, periods=(5,))

        self.assertIsNone(averages[5])

    def test_calculates_average_from_latest_period(self) -> None:
        values = [Decimal(value) for value in range(1, 11)]

        averages = moving_averages(values, periods=(5, 10))

        self.assertEqual(averages[5], Decimal("8"))
        self.assertEqual(averages[10], Decimal("5.5"))

    def test_daily_history_window_supports_ma240_prefetch(self) -> None:
        self.assertGreaterEqual(HISTORY_CALENDAR_DAYS, 600)

    def test_calculates_volume_averages_in_lots(self) -> None:
        volumes = [Decimal(value) for value in range(1, 21)]

        averages = moving_averages(volumes, periods=(5, 20))

        self.assertEqual(averages[5], Decimal("18"))
        self.assertEqual(averages[20], Decimal("10.5"))


if __name__ == "__main__":
    unittest.main()
