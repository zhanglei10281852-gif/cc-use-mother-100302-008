"""端到端业务测试：回避分配、评分决策、里程碑拨款、守恒与审计。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import unittest

from industry_fund import Clock, ConflictError, FundService, Money, Repository, StateError, ValidationError
from industry_fund.domain import MilestoneStatus


def base_application(**overrides):
    data = {
        "project_name": "人形机器人灵巧手项目",
        "kind": "tech_transfer",
        "tech_route": "基于高校触觉传感专利的三代路线，2027 年量产",
        "team": [
            {"name": "王教授", "org": "同源大学", "role": "技术负责人"},
            {"name": "李博士", "org": "自旋机器人公司", "role": "CEO"},
        ],
        "fund_usage": [
            {"purpose": "中试线建设", "amount": "600000000"},
            {"purpose": "团队与专利许可", "amount": "400000000"},
        ],
        "related_parties": [
            {"name": "同源大学", "relation": "成果依托单位", "party_type": "org"},
            {"name": "自旋机器人公司", "relation": "申请主体", "party_type": "org"},
        ],
        "requested_amount": "1000000000",
    }
    data.update(overrides)
    return data


DIMENSIONS = {"tech": 88, "team": 80, "usage": 85, "risk": 70}


class FundServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock(datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc))
        self.repo = Repository(":memory:")
        self.svc = FundService(self.repo, self.clock)
        self.svc.add_fund_source("a-gov", "政府引导基金", "1500000000")
        self.svc.add_fund_source("b-social", "社会资本", "800000000")
        for rid, name, aff in [
            ("r1", "张评委", []),
            ("r2", "赵评委", []),
            ("r3", "钱评委", []),
            ("r4", "孙评委", []),
            ("rc", "周评委", ["同源大学"]),
        ]:
            self.svc.register_reviewer(rid, name, ["机器人"], aff, [])

    def tearDown(self) -> None:
        self.repo.close()

    def advance(self, **delta) -> None:
        self.clock.freeze(self.clock.now() + timedelta(**delta))

    def create_reviewed_case(self, case_code="case-001", votes=None, conditions=None):
        self.svc.create_case(case_code, "自旋机器人公司", "2026秋季")
        self.svc.submit_application(case_code, base_application(), "applicant")
        self.svc.lock_for_review(case_code)
        assigned = self.svc.assign_reviewers(case_code, ["r1", "r2", "r3", "r4", "rc"])
        return assigned


class ApplicationAndAssignmentTests(FundServiceTestBase):
    def test_versions_are_immutable_and_dedup(self) -> None:
        self.svc.create_case("c1", "申请人", "round")
        first = self.svc.submit_application("c1", base_application(), "a")
        again = self.svc.submit_application("c1", base_application(), "a")
        self.assertEqual(first["version"], 1)
        self.assertTrue(again["deduplicated"])
        revised = self.svc.submit_application(
            "c1", base_application(tech_route="变更后的技术路线"), "a")
        self.assertEqual(revised["version"], 2)
        versions = self.svc.list_versions("c1")
        self.assertEqual(len(versions), 2)
        # 历史版本的技术路线保持原样。
        self.assertIn("三代路线", versions[0]["content"]["tech_route"])
        self.assertIn("变更后", versions[1]["content"]["tech_route"])

    def test_fund_usage_must_equal_requested(self) -> None:
        self.svc.create_case("c1", "申请人", "round")
        bad = base_application(fund_usage=[{"purpose": "x", "amount": "1"}])
        with self.assertRaises(ValidationError):
            self.svc.submit_application("c1", bad, "a")

    def test_conflicted_reviewer_is_ineligible_and_explained(self) -> None:
        assigned = self.create_reviewed_case()
        by_id = {a["reviewer_id"]: a for a in assigned["assignments"]}
        self.assertFalse(by_id["rc"]["eligible"])
        self.assertIn("同源大学", by_id["rc"]["reason"])
        self.assertEqual(assigned["eligible_reviewer_ids"], ["r1", "r2", "r3", "r4"])
        with self.assertRaises(StateError):
            self.svc.submit_score("case-001", "rc", DIMENSIONS, "我有利益关系但想打分")

    def test_min_panel_size_enforced(self) -> None:
        self.svc.create_case("small", "申请人", "round")
        # 团队成员姓名命中 r1/r2/r3，加上关联方命中 rc，仅剩 r4 一人合资格。
        team = list(base_application()["team"]) + [
            {"name": "张评委", "org": "", "role": "顾问"},
            {"name": "赵评委", "org": "", "role": "顾问"},
            {"name": "钱评委", "org": "", "role": "顾问"},
        ]
        self.svc.submit_application(
            "small", base_application(team=team), "a")
        self.svc.lock_for_review("small")
        with self.assertRaises(StateError):
            self.svc.assign_reviewers("small", ["r1", "r2", "r3", "r4", "rc"])

    def test_material_change_recuses_and_reopens_round(self) -> None:
        self.create_reviewed_case()
        self.svc.submit_score("case-001", "r1", DIMENSIONS, "技术扎实")
        self.advance(days=1)
        revised = self.svc.submit_application(
            "case-001",
            base_application(related_parties=base_application()["related_parties"] + [
                {"name": "某神秘机构", "relation": "新增持股方", "party_type": "org"}]),
            "applicant")
        self.assertEqual(revised["new_round"], 2)
        # r1 的旧分仍可查，但新一轮必须重新打分。
        detail = self.svc.get_case("case-001")
        self.assertEqual(len(detail["scores"]), 1)
        self.svc.submit_score("case-001", "r1", DIMENSIONS, "重新评估")


class DecisionAndAppealTests(FundServiceTestBase):
    def _panel_vote(self, code, votes=("approve", "approve", "conditional", "reject")):
        for rid, vote in zip(["r1", "r2", "r3", "r4"], votes):
            self.svc.submit_score(code, rid, DIMENSIONS, f"{rid} 的评分理由")
            self.svc.cast_ballot(code, rid, vote, f"{rid} 意见")

    def test_decision_commits_fund_and_is_explainable(self) -> None:
        self.create_reviewed_case()
        self._panel_vote("case-001")
        result = self.svc.decide("case-001", "ic", rationale="技术领先，额度核准")
        self.assertEqual(result["outcome"], "approve")
        self.assertEqual(result["tally"], {"approve": 2, "conditional": 1, "reject": 1})
        self.assertEqual(result["approved_amount"]["cents"], Money.yuan("1000000000").cents)
        # 可解释：每个评委的维度分、理由、权重与总分都在。
        breakdown = result["score_breakdown"]
        self.assertEqual(breakdown["weights"]["tech"], 0.40)
        self.assertIn("r1", breakdown["per_reviewer"])
        self.assertTrue(breakdown["per_reviewer"]["r1"]["rationale"])
        snap = self.svc.fund_snapshot()
        self.assertEqual(snap["committed"]["cents"], 1_000_000_000_00)
        self.assertEqual(snap["available"]["cents"], 1_300_000_000_00)

    def test_duplicate_ballot_rejected_even_concurrently(self) -> None:
        self.create_reviewed_case()
        self.svc.submit_score("case-001", "r1", DIMENSIONS, "理由")
        outcomes: list[Exception | None] = []

        def vote() -> None:
            try:
                self.svc.cast_ballot("case-001", "r1", "approve", "并发投票")
                outcomes.append(None)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=vote) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for o in outcomes if o is None), 1)
        self.assertFalse(any(not isinstance(o, (type(None), ConflictError)) for o in outcomes))

    def test_tie_vote_blocks_decision(self) -> None:
        self.create_reviewed_case()
        self._panel_vote("case-001", votes=("approve", "approve", "reject", "reject"))
        with self.assertRaises(StateError):
            self.svc.decide("case-001", "ic")

    def test_conditional_requires_conditions(self) -> None:
        self.create_reviewed_case()
        self._panel_vote("case-001", votes=("conditional",) * 4)
        with self.assertRaises(ValidationError):
            self.svc.decide("case-001", "ic")

    def test_appeal_upheld_reverses_commitment_and_allows_redecision(self) -> None:
        self.create_reviewed_case()
        self._panel_vote("case-001")
        self.svc.decide("case-001", "ic")
        appeal = self.svc.file_appeal("case-001", "回避名单遗漏，程序瑕疵", "applicant")
        self.svc.rule_appeal(appeal["appeal_id"], True, "申诉成立，重组评审组", "committee")
        snap = self.svc.fund_snapshot()
        self.assertEqual(snap["committed"]["cents"], 0)
        self.assertEqual(snap["available"]["cents"], 2_300_000_000_00)
        detail = self.svc.get_case("case-001")
        self.assertTrue(detail["decisions"][0]["void"])
        # 新一轮重新评分投票后可再决策。
        self._panel_vote("case-001")
        again = self.svc.decide("case-001", "ic")
        self.assertEqual(again["decision_version"], 2)
        self.assertEqual(self.svc.fund_snapshot()["committed"]["cents"], 1_000_000_000_00)


class MilestoneAndDisbursementTests(FundServiceTestBase):
    def _approved_case(self, code="case-h", outcome="approve", conditions=None):
        self.svc.create_case(code, "申请人", "round")
        self.svc.submit_application(code, base_application(), "a")
        self.svc.lock_for_review(code)
        self.svc.assign_reviewers(code, ["r1", "r2", "r3"])
        for rid in ["r1", "r2", "r3"]:
            self.svc.submit_score(code, rid, DIMENSIONS, f"{rid} 理由")
            self.svc.cast_ballot(code, rid, outcome, "同意")
        decision = self.svc.decide(
            code, "ic", conditions=conditions, rationale="决议") if conditions or outcome == "conditional" \
            else self.svc.decide(code, "ic")
        return decision

    def _contract(self, code="case-h"):
        return self.svc.sign_contract(code, [
            {"seq": 1, "name": "中试线验收", "criteria": "良率>=95%", "amount": "600000000"},
            {"seq": 2, "name": "量产交付", "criteria": "出货1000套", "amount": "400000000"},
        ], "ic")

    def test_contract_milestones_must_sum_to_approved(self) -> None:
        self._approved_case()
        with self.assertRaises(ValidationError):
            self.svc.sign_contract("case-h", [
                {"seq": 1, "name": "m1", "criteria": "x", "amount": "999999999"},
                {"seq": 2, "name": "m2", "criteria": "y", "amount": "2"},
            ], "ic")

    def test_full_release_flow_with_idempotency(self) -> None:
        self._approved_case()
        self._contract()
        self.svc.submit_evidence("case-h", 1, "s3://evidence/m1.zip", "pm")
        self.svc.tech_review("case-h", 1, True, "tech-report-1", "cto", "良率达标")
        self.svc.finance_review("case-h", 1, True, "fin-report-1", "cfo")
        self.advance(days=2)
        paid = self.svc.disburse("case-h", 1, "pay-key-1", "treasury", "REF-1")
        self.assertEqual(paid["amount_cents"], 600_000_000_00)
        self.assertFalse(paid["replayed"])
        # 重复请求：同一幂等键返回同一结果，不产生第二笔。
        replay = self.svc.disburse("case-h", 1, "pay-key-1", "treasury", "REF-1")
        self.assertTrue(replay["replayed"])
        # 不带幂等键的重复支付也被状态拦截。
        with self.assertRaises(ConflictError):
            self.svc.disburse("case-h", 1, "pay-key-OTHER", "treasury")
        snap = self.svc.fund_snapshot()
        self.assertEqual(snap["paid"]["cents"], 600_000_000_00)
        self.assertEqual(snap["committed"]["cents"], 400_000_000_00)
        # 按日期回看：拨款发生在 10-07，10-05 的视图无已付，10-07 的视图含已付。
        self.assertEqual(self.svc.fund_snapshot("2026-10-05")["paid"]["cents"], 0)
        self.assertEqual(self.svc.fund_snapshot("2026-10-07")["paid"]["cents"],
                         600_000_000_00)

    def test_concurrent_disburse_pays_exactly_once(self) -> None:
        self._approved_case()
        self._contract()
        self.svc.submit_evidence("case-h", 1, "ev", "pm")
        self.svc.tech_review("case-h", 1, True, "tr", "cto")
        self.svc.finance_review("case-h", 1, True, "fr", "cfo")
        results: list[dict] = []
        errors: list[Exception] = []

        def pay() -> None:
            try:
                results.append(self.svc.disburse("case-h", 1, "same-key", "t"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=pay) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        real = [r for r in results if not r.get("replayed")]
        self.assertEqual(len(real), 1)
        self.assertEqual(self.svc.fund_snapshot()["paid"]["cents"], 600_000_000_00)

    def test_condition_gates_finance_and_disbursement(self) -> None:
        self._approved_case(outcome="conditional", conditions=[
            {"description": "完成专利独占许可备案", "milestone_seq": 1}])
        self._contract()
        self.svc.submit_evidence("case-h", 1, "ev", "pm")
        self.svc.tech_review("case-h", 1, True, "tr", "cto")
        with self.assertRaises(StateError):
            self.svc.finance_review("case-h", 1, True, "fr", "cfo")
        self.svc.clear_condition("case-h", "c1", "ic")
        self.svc.finance_review("case-h", 1, True, "fr", "cfo")
        paid = self.svc.disburse("case-h", 1, "k1", "t")
        self.assertEqual(paid["status"], "paid")

    def test_tech_failure_terminates_and_freezes_remaining(self) -> None:
        self._approved_case(code="case-f")
        self.svc.sign_contract("case-f", [
            {"seq": 1, "name": "m1", "criteria": "c", "amount": "1000000000"}], "ic")
        self.svc.submit_evidence("case-f", 1, "ev", "pm")
        result = self.svc.tech_review("case-f", 1, False, "tr", "cto", "指标未达")
        self.assertTrue(result["terminated"])
        detail = self.svc.get_case("case-f")
        self.assertEqual(detail["milestones"][0]["status"], MilestoneStatus.FAILED)
        self.assertEqual(detail["state"], "terminated")
        snap = self.svc.fund_snapshot()
        self.assertEqual(snap["frozen"]["cents"], 1_000_000_000_00)
        self.assertEqual(snap["committed"]["cents"], 0)
        # 争议结清后冻结资金回收为可用。
        self.svc.defrost("case-f", "risk")
        snap2 = self.svc.fund_snapshot()
        self.assertEqual(snap2["frozen"]["cents"], 0)
        self.assertEqual(snap2["available"]["cents"], 2_300_000_000_00)

    def test_recall_then_terminate_keeps_conservation(self) -> None:
        self._approved_case()
        self._contract()
        self.svc.submit_evidence("case-h", 1, "ev", "pm")
        self.svc.tech_review("case-h", 1, True, "tr", "cto")
        self.svc.finance_review("case-h", 1, True, "fr", "cfo")
        before_pay = self.clock.now()
        self.advance(hours=1)
        self.svc.disburse("case-h", 1, "k1", "t")
        self.svc.recall_disbursement("case-h", 1, "事后复核发现造假", "risk")
        self.svc.terminate("case-h", "企业诚信问题终止", "risk")
        snap = self.svc.fund_snapshot()
        # 600m 撤回冻结 + 400m 未释放取消冻结 = 10 亿，全部未支付。
        self.assertEqual(snap["paid"]["cents"], 0)
        self.assertEqual(snap["frozen"]["cents"], 1_000_000_000_00)
        self.svc.defrost("case-h", "risk")
        # 支付前日期的快照不应包含该笔付款。
        old = self.svc.fund_snapshot(before_pay.isoformat())
        self.assertEqual(old["paid"]["cents"], 0)
        self.assertEqual(old["committed"]["cents"], 1_000_000_000_00)

    def test_terminate_before_contract_freezes_full_commitment(self) -> None:
        self._approved_case(code="case-t")
        # 决策后未签合同即终止，全额承诺冻结。
        result = self.svc.terminate("case-t", "投决后发现尽调重大遗漏", "ic")
        self.assertEqual(result["cancelled_cents"], 1_000_000_000_00)
        snap = self.svc.fund_snapshot()
        self.assertEqual(snap["frozen"]["cents"], 1_000_000_000_00)
        self.assertEqual(snap["committed"]["cents"], 0)

    def test_double_recall_rejected(self) -> None:
        self._approved_case()
        self._contract()
        self.svc.submit_evidence("case-h", 1, "ev", "pm")
        self.svc.tech_review("case-h", 1, True, "tr", "cto")
        self.svc.finance_review("case-h", 1, True, "fr", "cfo")
        self.svc.disburse("case-h", 1, "k1", "t")
        self.svc.recall_disbursement("case-h", 1, "理由", "risk")
        with self.assertRaises(ConflictError):
            self.svc.recall_disbursement("case-h", 1, "再次撤回", "risk")


class QuestionsAndAuditTests(FundServiceTestBase):
    def test_question_response_is_append_only(self) -> None:
        self.svc.create_case("c1", "申请人", "round")
        self.svc.submit_application("c1", base_application(), "a")
        self.svc.lock_for_review("c1")
        self.svc.assign_reviewers("c1", ["r1", "r2", "r3"])
        q = self.svc.ask_question("c1", "r1", "专利许可范围？")
        self.svc.respond_question(q["question_id"], "独占许可五年", "applicant")
        with self.assertRaises(ConflictError):
            self.svc.respond_question(q["question_id"], "想改写答复", "applicant")

    def test_audit_hash_chain_detects_tampering(self) -> None:
        self.svc.create_case("c1", "申请人", "round")
        self.svc.submit_application("c1", base_application(), "a")
        self.assertTrue(self.svc.verify()["audit_chain_intact"])
        # 直接篡改底层历史。
        self.repo.conn.execute("UPDATE audit_log SET actor='ghost' WHERE seq=1")
        self.assertFalse(self.svc.verify()["audit_chain_intact"])


class MoneyTests(unittest.TestCase):
    def test_yuan_string_avoids_float_error(self) -> None:
        self.assertEqual(Money.yuan("1000000000.99").cents, 100_000_000_099)
        self.assertEqual((Money.yuan("2.50") - Money.yuan("1.20")).cents, 130)
        with self.assertRaises(ValueError):
            Money(-1)


class MultiSourceLedgerTests(FundServiceTestBase):
    def test_payment_splits_across_sources_and_snapshot_tracks_each(self) -> None:
        # 申请 20 亿：a-gov 剩 15 亿，b-social 8 亿，承诺与支付应跨来源拆分。
        self.svc.create_case("case-ms", "申请人", "round")
        self.svc.submit_application(
            "case-ms",
            base_application(
                fund_usage=[{"purpose": "建设", "amount": "2000000000"}],
                requested_amount="2000000000"), "a")
        self.svc.lock_for_review("case-ms")
        self.svc.assign_reviewers("case-ms", ["r1", "r2", "r3"])
        for rid in ["r1", "r2", "r3"]:
            self.svc.submit_score("case-ms", rid, DIMENSIONS, "理由")
            self.svc.cast_ballot("case-ms", rid, "approve", "同意")
        decision = self.svc.decide("case-ms", "ic")
        alloc = {a["source"]: a["cents"] for a in decision["allocations"]}
        self.assertEqual(alloc, {"a-gov": 1_500_000_000_00, "b-social": 500_000_000_00})
        self.svc.sign_contract("case-ms", [
            {"seq": 1, "name": "m1", "criteria": "c", "amount": "2000000000"}], "ic")
        self.svc.submit_evidence("case-ms", 1, "ev", "pm")
        self.svc.tech_review("case-ms", 1, True, "tr", "cto")
        self.svc.finance_review("case-ms", 1, True, "fr", "cfo")
        paid = self.svc.disburse("case-ms", 1, "k", "t")
        paid_sources = {a["source"]: a["cents"] for a in paid["allocations"]}
        self.assertEqual(paid_sources,
                         {"a-gov": 1_500_000_000_00, "b-social": 500_000_000_00})
        snap = self.svc.fund_snapshot()
        self.assertEqual(snap["paid"]["cents"], 2_000_000_000_00)
        self.assertEqual(snap["by_source"]["a-gov"]["paid"], 1_500_000_000_00)
        self.assertEqual(snap["by_source"]["b-social"]["paid"], 500_000_000_00)
        self.assertEqual(snap["by_source"]["b-social"]["available"], 300_000_000_00)
        self.assertTrue(self.svc.verify()["ledger_conserved"])


if __name__ == "__main__":
    unittest.main()
