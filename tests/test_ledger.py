"""台账：复式平衡、来源 FIFO 追溯、多项目隔离。"""

import unittest

from industry_fund import to_yuan
from industry_fund.ledger import (
    ACC_AVAILABLE, ACC_COMMITTED, ACC_FROZEN, ACC_PAID, ACC_SOURCE,
    project_entries, snapshot,
)

from tests._support import make_service, seed_fund, seed_reviewers, approved_case


def _src_map(snap):
    return {s["source_id"]: s for s in snap.by_source}


class LedgerBalanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        seed_fund(self.svc)

    def test_fund_establishment_is_balanced(self) -> None:
        entries = project_entries(self.store.load_all())
        debits = sum(e.debit_cents for e in entries)
        credits = sum(e.credit_cents for e in entries)
        self.assertEqual(debits, credits)
        self.assertEqual(debits, 200_000_000_000)
        snap = snapshot(entries, "fund-1")
        self.assertEqual(snap.available_cents, 200_000_000_000)

    def test_overcommitment_rejected(self) -> None:
        seed_reviewers(self.svc)
        approved_case(self.svc, requested="2000000000")
        with self.assertRaises(Exception):
            self.svc.sign_contract("case-1", "fund-1", [
                {"milestone_id": "m1", "name": "x", "amount": "2000000001",
                 "criteria": "c"}], actor="admin")
        # 失败命令不产生任何资金事件，余额不变
        snap = snapshot(project_entries(self.store.load_all()), "fund-1")
        self.assertEqual(snap.available_cents, 200_000_000_000)
        self.assertEqual(snap.committed_cents, 0)


class FIFOsourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        seed_fund(self.svc)  # gov 12亿, lp 8亿
        seed_reviewers(self.svc)
        approved_case(self.svc, case_id="c1", code="C-1", requested="1300000000")
        self.svc.sign_contract("c1", "fund-1", [
            {"milestone_id": "m1", "name": "一", "amount": "1100000000", "criteria": "x"},
            {"milestone_id": "m2", "name": "二", "amount": "200000000", "criteria": "x"},
        ], actor="admin")

    def _snap(self):
        return snapshot(project_entries(self.store.load_all()), "fund-1")

    def _pass(self, mid, eligible=None):
        self.svc.submit_evidence(mid, [{"name": "r", "hash": f"h-{mid}"}], "s",
                                 actor="企")
        self.svc.review_technical(mid, True, "ok", reviewer="t")
        if eligible:
            self.svc.review_financial(mid, True, "ok", reviewer="f",
                                      eligible_amount=eligible)
        else:
            self.svc.review_financial(mid, True, "ok", reviewer="f")

    def test_commitment_spans_sources_fifo(self) -> None:
        src = _src_map(self._snap())
        # gov 12亿全部占用，m2 中有 1亿来自 lp
        self.assertEqual(src["gov"]["committed_cents"], 120_000_000_000)
        self.assertEqual(src["lp"]["committed_cents"], 10_000_000_000)
        self.assertEqual(src["lp"]["available_cents"], 70_000_000_000)

    def test_payment_then_clawback_returns_to_original_source(self) -> None:
        self._pass("m1")
        self.svc.release_payment("m1", "P-1", actor="fa")
        src = _src_map(self._snap())
        self.assertEqual(src["gov"]["paid_cents"], 110_000_000_000)
        self.svc.clawback_payment("m1", "追回", actor="admin")
        src = _src_map(self._snap())
        self.assertEqual(src["gov"]["paid_cents"], 0)
        # m2 仍占用 gov 1 亿承诺，故 m1 的 11 亿回到 gov 可用
        self.assertEqual(src["gov"]["available_cents"], 110_000_000_000)
        self.assertEqual(src["gov"]["committed_cents"], 10_000_000_000)

    def test_financial_reduction_on_boundary_milestone(self) -> None:
        # m2 跨来源（gov 1亿 + lp 1亿）；核减到 1.5亿：
        # 先释放差额 5000万（FIFO 取 gov），再冻结剩余 gov 5000万 + lp 1亿
        self._pass("m2", eligible="150000000")
        src = _src_map(self._snap())
        self.assertEqual(src["gov"]["frozen_cents"], 5_000_000_000)
        self.assertEqual(src["lp"]["frozen_cents"], 10_000_000_000)
        self.assertEqual(src["gov"]["available_cents"], 5_000_000_000)


class MultiCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        seed_fund(self.svc)
        seed_reviewers(self.svc)

    def test_two_cases_commitments_and_failures_are_isolated(self) -> None:
        approved_case(self.svc, case_id="c1", code="C-1", requested="100000000")
        approved_case(self.svc, case_id="c2", code="C-2", requested="200000000")
        self.svc.sign_contract("c1", "fund-1", [
            {"milestone_id": "m1", "name": "a", "amount": "100000000", "criteria": "x"}],
            actor="admin")
        self.svc.sign_contract("c2", "fund-1", [
            {"milestone_id": "m2", "name": "b", "amount": "200000000", "criteria": "x"}],
            actor="admin")
        snap = snapshot(project_entries(self.store.load_all()), "fund-1")
        by_case = {c["case_id"]: c for c in snap.by_case}
        self.assertEqual(by_case["c1"]["committed_cents"], 10_000_000_000)
        self.assertEqual(by_case["c2"]["committed_cents"], 20_000_000_000)
        # c1 里程碑失败只释放 c1
        self.svc.submit_evidence("m1", [{"name": "r", "hash": "h"}], "s", actor="企")
        self.svc.review_technical("m1", False, "失败", reviewer="t")
        snap = snapshot(project_entries(self.store.load_all()), "fund-1")
        by_case = {c["case_id"]: c for c in snap.by_case}
        self.assertEqual(by_case["c1"]["committed_cents"], 0)
        self.assertEqual(by_case["c2"]["committed_cents"], 20_000_000_000)
        snap.check_conservation()


if __name__ == "__main__":
    unittest.main()
