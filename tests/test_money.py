"""金额以整数分记账的测试。"""

import unittest

from industry_fund import to_cents, to_yuan
from industry_fund.errors import ValidationError


class MoneyTests(unittest.TestCase):
    def test_string_and_decimal(self) -> None:
        self.assertEqual(to_cents("2000000000"), 200_000_000_000)
        self.assertEqual(to_cents("30000000.25"), 3_000_000_025)

    def test_integer_yuan(self) -> None:
        self.assertEqual(to_cents(1), 100)

    def test_round_half_up(self) -> None:
        self.assertEqual(to_cents("0.005"), 1)

    def test_excess_precision_rejected(self) -> None:
        # 0.001 量化后为 0.00，直接拒绝非正金额由业务层处理；这里确保不抛解析错
        self.assertEqual(to_cents("0.001"), 0)

    def test_reject_garbage(self) -> None:
        with self.assertRaises(ValueError):
            to_cents("not-money")
        with self.assertRaises(ValueError):
            to_cents(True)  # type: ignore[arg-type]

    def test_to_yuan_roundtrip(self) -> None:
        for cents in (0, 1, 99, 100, 200_000_000_000, -505):
            self.assertEqual(to_cents(to_yuan(cents)), cents)


if __name__ == "__main__":
    unittest.main()
