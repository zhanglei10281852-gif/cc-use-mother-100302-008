"""里程碑拨款：证据、双重复核、冻结、付款、失败阻断、撤回、终止。"""

import unittest

from industry_fund.errors import (
    ConflictStateError, IdempotencyConflict, ValidationError,
)
from industry_fund import to_yuan

from tests._support import make_service, contracted_case


def pass_milestone(svc, mid, summary="通过", eligible=None):
    svc.submit_evidence(mid, [{"name": "report.pdf", "hash": f"hash-{mid}"}],
                        summary, actor="企业")
    svc.review_technical(mid, True, "技术达标", reviewer="tech")
    if eligible is None:
        svc.review_financial(mid, True, "费用真实", reviewer="fin")
    else:
        svc.review_financial(mid, True, "部分核减", reviewer="fin",
                             eligible_amount=eligible)


class MilestoneGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        contracted_case(self.svc)

    def test_review_order_is_enforced(self) -> None:
        with self.assertRaises(ConflictStateError):
            self.svc.review_technical("ms-1", True, "无证据", reviewer="tech")
        self.svc.submit_evidence("ms-1", [{"name": "r", "hash": "h"}], "证据",
                                 actor="企业")
        with self.assertRaises(ConflictStateError):
            self.svc.review_financial("ms-1", True, "技术未复核", reviewer="fin")
        self.svc.review_technical("ms-1", True, "ok", reviewer="tech")
        # 复核结论不可改写
        with self.assertRaises(ConflictStateError):
            self.svc.review_technical("ms-1", False, "改判", reviewer="tech")
        self.svc.review_financial("ms-1", True, "ok", reviewer="fin")
        # 双复核通过并冻结后才能付款
        self.assertEqual(self.svc.milestone_view("ms-1")["status"], "reviewed")
        self.svc.release_payment("ms-1", "PAY-1", actor="fa")
        self.assertEqual(self.svc.milestone_view("ms-1")["status"], "paid")
        # 同里程碑不能第二次付款
        with self.assertRaises(ConflictStateError):
            self.svc.release_payment("ms-1", "PAY-2", actor="fa")

    def test_duplicate_payment_ref_never_pays_twice(self) -> None:
        pass_milestone(self.svc, "ms-1")
        r1 = self.svc.release_payment("ms-1", "PAY-DUP", actor="fa", idem_key="k1")
        self.assertFalse(r1["replayed"])
        r2 = self.svc.release_payment("ms-1", "PAY-DUP", actor="fa", idem_key="k1")
        self.assertTrue(r2["replayed"])
        self.assertEqual(len(r2["events"]), 1)
        pos = self.svc.fund_position("fund-1")
        self.assertEqual(pos["paid_cents"], 3_000_000_000)

    def test_idempotency_key_reused_with_different_body_conflicts(self) -> None:
        pass_milestone(self.svc, "ms-1")
        self.svc.release_payment("ms-1", "PAY-A", actor="fa", idem_key="same")
        with self.assertRaises(IdempotencyConflict):
            self.svc.release_payment("ms-1", "PAY-B", actor="fa", idem_key="same")

    def test_technical_failure_blocks_all_further_disbursement(self) -> None:
        self.svc.submit_evidence("ms-2", [{"name": "r", "hash": "h2"}], "中试",
                                 actor="企业")
        self.svc.review_technical("ms-2", False, "良率不达标", reviewer="tech")
        m = self.svc.milestone_view("ms-2")
        self.assertEqual(m["status"], "failed")
        for action in ("evidence",):
            with self.assertRaises(ConflictStateError):
                self.svc.submit_evidence("ms-2", [{"name": "x", "hash": "y"}],
                                         "补证据", actor="企业")
        with self.assertRaises(ConflictStateError):
            self.svc.review_financial("ms-2", True, "失败里程碑不能财务通过",
                                      reviewer="fin")
        with self.assertRaises(ConflictStateError):
            self.svc.release_payment("ms-2", "PAY-X", actor="fa")
        # 失败里程碑的承诺额度已立即释放
        pos = self.svc.fund_position("fund-1")
        # ms-1 3000万 + ms-3 4000万 仍承诺/冻结，ms-2 的 3000万已释放
        self.assertEqual(pos["committed_cents"] + pos["frozen_cents"], 7_000_000_000)

    def test_failure_flag_on_case_freezes_disbursement(self) -> None:
        pass_milestone(self.svc, "ms-2")  # 冻结但未付
        self.svc.flag_failure("case-1", "发现数据造假", actor="monitor")
        with self.assertRaises(ConflictStateError):
            self.svc.release_payment("ms-2", "PAY-MS2", actor="fa")
        with self.assertRaises(ConflictStateError):
            self.svc.submit_evidence("ms-3", [{"name": "r", "hash": "h3"}],
                                     "量产证据", actor="企业")
        # 解除后可继续
        self.svc.waive_failure("case-1", "核查后排除", actor="committee")
        self.svc.release_payment("ms-2", "PAY-MS2", actor="fa")

    def test_financial_reduction_releases_difference(self) -> None:
        self.svc.submit_evidence("ms-1", [{"name": "r", "hash": "h"}], "证据",
                                 actor="企业")
        self.svc.review_technical("ms-1", True, "ok", reviewer="tech")
        self.svc.review_financial("ms-1", True, "核减500万", reviewer="fin",
                                  eligible_amount="25000000")
        m = self.svc.milestone_view("ms-1")
        self.assertEqual(m["financial"]["approved_cents"], 2_500_000_000)
        pos = self.svc.fund_position("fund-1")
        self.assertEqual(pos["frozen_cents"], 2_500_000_000)
        self.svc.release_payment("ms-1", "PAY-RED", actor="fa")
        self.assertEqual(self.svc.fund_position("fund-1")["paid_cents"], 2_500_000_000)


class ClawbackAndTerminationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        contracted_case(self.svc)
        pass_milestone(self.svc, "ms-1")
        self.svc.release_payment("ms-1", "PAY-1", actor="fa")

    def test_partial_then_full_clawback(self) -> None:
        self.svc.clawback_payment("ms-1", "违规追回1000万", actor="admin",
                                  amount="10000000")
        pos = self.svc.fund_position("fund-1")
        self.assertEqual(pos["paid_cents"], 2_000_000_000)
        m = self.svc.milestone_view("ms-1")
        self.assertFalse(m["clawback"]["full"])
        self.svc.clawback_payment("ms-1", "追回余款", actor="admin")
        pos = self.svc.fund_position("fund-1")
        self.assertEqual(pos["paid_cents"], 0)
        self.assertTrue(self.svc.milestone_view("ms-1")["clawback"]["full"])
        with self.assertRaises(ConflictStateError):
            self.svc.clawback_payment("ms-1", "超额", actor="admin", amount="1")

    def test_clawback_is_idempotent(self) -> None:
        self.svc.clawback_payment("ms-1", "追回", actor="admin", idem_key="cb1")
        result = self.svc.clawback_payment("ms-1", "追回", actor="admin", idem_key="cb1")
        self.assertTrue(result["replayed"])
        self.assertEqual(self.svc.fund_position("fund-1")["paid_cents"], 0)

    def test_termination_releases_all_unpaid_and_conserves(self) -> None:
        # ms-1 已付 3000万；ms-2/ms-3 未动
        self.svc.terminate_case("case-1", "路线终止", actor="admin")
        view = self.svc.case_view("case-1")
        self.assertEqual(view["status"], "terminated")
        statuses = {m["milestone_id"]: m["status"] for m in view["milestones"]}
        self.assertEqual(statuses["ms-2"], "cancelled")
        self.assertEqual(statuses["ms-3"], "cancelled")
        self.assertEqual(statuses["ms-1"], "paid")
        pos = self.svc.fund_position("fund-1")
        self.assertEqual(pos["committed_cents"], 0)
        self.assertEqual(pos["frozen_cents"], 0)
        self.assertEqual(pos["paid_cents"], 3_000_000_000)
        self.assertEqual(pos["available_cents"],
                         pos["total_cents"] - 3_000_000_000)
        # 终止后不能再付款
        with self.assertRaises(ConflictStateError):
            self.svc.release_payment("ms-2", "PAY-LATE", actor="fa")

    def test_termination_with_frozen_milestone_unfreezes_first(self) -> None:
        pass_milestone(self.svc, "ms-2")  # 冻结 3000万
        self.svc.terminate_case("case-1", "终止", actor="admin")
        pos = self.svc.fund_position("fund-1")
        self.assertEqual(pos["frozen_cents"], 0)
        self.assertEqual(pos["committed_cents"], 0)
        # 审计链中存在 解冻->释放->取消 的留痕顺序
        trail = self.svc.audit_trail("ms-2")
        types = [e["type"] for e in trail]
        self.assertIn("MilestoneUnfrozen", types)
        self.assertIn("MilestoneCancelled", types)
        self.assertLess(types.index("MilestoneUnfrozen"),
                        types.index("MilestoneCancelled"))

    def test_payment_after_termination_then_clawback_conserves(self) -> None:
        self.svc.terminate_case("case-1", "终止", actor="admin")
        self.svc.clawback_payment("ms-1", "全额追回", actor="admin")
        pos = self.svc.fund_position("fund-1")
        self.assertEqual(pos["paid_cents"], 0)
        self.assertEqual(pos["available_cents"], pos["total_cents"])


class AsOfPositionTests(unittest.TestCase):
    def setUp(self) -> None:
        from datetime import datetime, timezone, timedelta
        self.base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.svc, self.store, self.path = make_service(clock=lambda: self.now)
        self.now = self.base
        contracted_case(self.svc)
        self.now += timedelta(days=40)
        pass_milestone(self.svc, "ms-1")
        self.svc.release_payment("ms-1", "PAY-1", actor="fa")

    def test_position_at_earlier_dates(self) -> None:
        before = self.svc.fund_position("fund-1", as_of="2026-09-30")
        self.assertEqual(before["total_cents"], 0)
        at_funding = self.svc.fund_position("fund-1", as_of="2026-10-02")
        self.assertEqual(at_funding["total_cents"], 200_000_000_000)
        # 合同在 base 当天签署：10-01 视图含承诺（合同总额 1 亿元）
        at_contract = self.svc.fund_position("fund-1", as_of="2026-10-01")
        self.assertEqual(at_contract["committed_cents"], 10_000_000_000)
        after_pay = self.svc.fund_position("fund-1", as_of="2026-11-15")
        self.assertEqual(after_pay["paid_cents"], 3_000_000_000)
        latest = self.svc.fund_position("fund-1")
        self.assertEqual(latest["paid_cents"], 3_000_000_000)


if __name__ == "__main__":
    unittest.main()
