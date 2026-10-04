"""机器人产业基金里程碑拨款基础契约测试。"""

import unittest

from industry_fund import InvestmentCase, unique_by_identity


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {'case_code': 'case-code-001', 'applicant': 'applicant-001', 'round_name': 'round-name-001', 'state': 'draft'}

    def test_fingerprint_is_stable(self) -> None:
        left = InvestmentCase(**self.values)
        right = InvestmentCase(**dict(reversed(list(self.values.items()))))
        self.assertEqual(left.fingerprint(), right.fingerprint())

    def test_evolve_keeps_original(self) -> None:
        original = InvestmentCase(**self.values)
        change_key = next(key for key, value in self.values.items() if isinstance(value, str))
        changed = original.evolve(**{change_key: "revised-value"})
        self.assertNotEqual(original.fingerprint(), changed.fingerprint())
        self.assertEqual(getattr(original, change_key), self.values[change_key])

    def test_conflicting_identity_is_rejected(self) -> None:
        first = InvestmentCase(**self.values)
        changed_values = dict(self.values)
        change_key = next(key for key in self.values if key != "case_code")
        changed_values[change_key] = 2 if isinstance(changed_values[change_key], int) else "conflict"
        second = InvestmentCase(**changed_values)
        with self.assertRaises(ValueError):
            unique_by_identity([first, second])


if __name__ == "__main__":
    unittest.main()
