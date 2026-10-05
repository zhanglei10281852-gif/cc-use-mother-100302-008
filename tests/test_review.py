"""项目评审：材料版本、回避分配、评分质询、条件决策与申诉。"""

import unittest

from industry_fund.errors import (
    ConflictOfInterestError, ConflictStateError, NotFoundError, ValidationError,
)

from tests._support import make_service, seed_reviewers, approved_case


class ApplicationVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()

    def test_sections_required_and_versioned_with_hash_chain(self) -> None:
        seed_reviewers(self.svc)
        self.svc.create_case("c1", "C-1", "core_components", "甲", "甲公司", "1000", actor="甲")
        with self.assertRaises(ValidationError):
            self.svc.submit_application(
                "c1", "", [{"name": "甲"}], {"x": 1},
                [{"party": "乙", "relationship": "供应商"}], actor="甲")
        self.svc.submit_application(
            "c1", "路线v1", [{"name": "甲"}], {"设备": 1000},
            [{"party": "乙", "relationship": "供应商"}],
            version_label="v1", actor="甲")
        self.svc.submit_application(
            "c1", "路线v2", [{"name": "甲", "role": "负责人"}], {"设备": 800},
            [], version_label="v2", change_summary="补充团队角色", actor="甲")
        view = self.svc.case_view("c1")
        self.assertEqual([v["version_no"] for v in view["application_versions"]], [1, 2])
        v1, v2 = view["application_versions"]
        self.assertNotEqual(v1["hash"], v2["hash"])
        self.assertEqual(v2["prev_version_hash"], v1["hash"])
        # v1 内容未被改写
        self.assertEqual(v1["technical_route"], "路线v1")

    def test_material_change_blocked_after_review_started(self) -> None:
        seed_reviewers(self.svc)
        approved_seed = dict(
            case_id="c2", code="C-2", applicant="乙", org="乙公司", requested="1000",
            parties=[])
        self.svc.create_case("c2", "C-2", "scenario_operations", "乙", "乙公司",
                             "1000", actor="乙")
        self.svc.submit_application("c2", "路线", [{"name": "乙"}], {"a": 1},
                                    [], actor="乙")
        self.svc.open_review_round("c2")
        self.svc.assign_reviewers("c2")
        with self.assertRaises(ConflictStateError):
            self.svc.submit_application("c2", "新材料", [{"name": "乙"}], {"a": 1},
                                        [], actor="乙")


class AssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        seed_reviewers(self.svc)
        self.svc.create_case("c1", "C-1", "university_transfer", "陈", "清华",
                             "5000", actor="陈")
        self.svc.submit_application(
            "c1", "路线", [{"name": "陈"}], {"x": 1},
            [{"party": "智元公司", "relationship": "持股"}], actor="陈")
        self.svc.open_review_round("c1")

    def test_org_and_declared_relationships_are_excluded(self) -> None:
        self.svc.assign_reviewers("c1", min_reviewers=3)
        rnd = self.svc.case_view("c1")["rounds"][-1]
        ids = {a["reviewer_id"] for a in rnd["assignments"]}
        excluded = {e["reviewer_id"]: e["reason"] for e in rnd["excluded"]}
        # rv-b 申报了与智元公司的关系
        self.assertIn("rv-b", excluded)
        self.assertNotIn("rv-b", ids)
        # 所有申请人/关联方均不作为评委
        self.assertTrue(ids)
        self.assertTrue(all("rv-b" != i for i in ids))

    def test_forcing_conflicted_reviewer_is_refused(self) -> None:
        with self.assertRaises(ConflictOfInterestError):
            self.svc.assign_reviewers("c1", reviewer_ids=["rv-b", "rv-c", "rv-d"])

    def test_unknown_and_inactive_reviewer_rejected(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.assign_reviewers("c1", reviewer_ids=["nope", "rv-c", "rv-d"])
        self.svc.deactivate_reviewer("rv-a", "离职", actor="admin")
        with self.assertRaises(ConflictStateError):
            self.svc.assign_reviewers("c1", min_reviewers=4)

    def test_double_assignment_rejected(self) -> None:
        self.svc.assign_reviewers("c1")
        with self.assertRaises(ConflictStateError):
            self.svc.assign_reviewers("c1")


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        seed_reviewers(self.svc)
        self.svc.create_case("c1", "C-1", "core_components", "甲", "甲公司",
                             "5000", actor="甲")
        self.svc.submit_application("c1", "路线", [{"name": "甲"}], {"x": 1},
                                    [{"party": "智元公司", "relationship": "供应链"}],
                                    actor="甲")
        self.svc.open_review_round("c1")
        self.svc.assign_reviewers("c1")
        self.assigned = [a["reviewer_id"]
                         for a in self.svc.case_view("c1")["rounds"][-1]["assignments"]]

    def test_unassigned_reviewer_cannot_vote(self) -> None:
        with self.assertRaises(ConflictOfInterestError):
            self.svc.submit_score(
                "c1", "rv-b",
                {"technology": 90, "team": 90, "market": 90, "compliance": 90})

    def test_score_range_and_dimensions(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.submit_score("c1", self.assigned[0],
                                  {"technology": 101, "team": 80, "market": 80,
                                   "compliance": 80})
        with self.assertRaises(ValidationError):
            self.svc.submit_score("c1", self.assigned[0],
                                  {"technology": 80, "team": 80, "market": 80})

    def test_score_is_immutable_and_explained(self) -> None:
        rid = self.assigned[0]
        self.svc.submit_score("c1", rid,
                              {"technology": 80, "team": 80, "market": 80,
                               "compliance": 80}, comment="初评")
        with self.assertRaises(ConflictStateError):
            self.svc.submit_score("c1", rid,
                                  {"technology": 90, "team": 90, "market": 90,
                                   "compliance": 90})
        score = self.svc.case_view("c1")["rounds"][-1]["scores"][0]
        self.assertEqual(score["weighted_total"], 80.0)
        self.assertEqual(set(score["weights"]),
                         {"technology", "team", "market", "compliance"})

    def test_abstention_counts_and_blocks_late_score(self) -> None:
        rid = self.assigned[0]
        self.svc.abstain_score("c1", rid, "发现亲属关系，主动回避")
        with self.assertRaises(ConflictStateError):
            self.svc.submit_score("c1", rid,
                                  {"technology": 80, "team": 80, "market": 80,
                                   "compliance": 80})

    def test_close_requires_all_votes_and_answered_questions(self) -> None:
        rid0, rid1, rid2 = self.assigned
        self.svc.submit_score("c1", rid0,
                              {"technology": 80, "team": 80, "market": 80,
                               "compliance": 80})
        with self.assertRaises(ConflictStateError):
            self.svc.close_round("c1")  # 还有评委未表决
        self.svc.submit_score("c1", rid1,
                              {"technology": 80, "team": 80, "market": 80,
                               "compliance": 80})
        self.svc.ask_question("c1", rid0, "问题1")
        self.svc.submit_score("c1", rid2,
                              {"technology": 80, "team": 80, "market": 80,
                               "compliance": 80})
        with self.assertRaises(ConflictStateError):
            self.svc.close_round("c1")  # 质询未答复
        qid = self.svc.case_view("c1")["rounds"][-1]["questions"][0]["question_id"]
        self.svc.answer_question("c1", qid, "答复", actor="甲")
        self.svc.close_round("c1")
        rnd = self.svc.case_view("c1")["rounds"][-1]
        self.assertEqual(rnd["status"], "closed")
        self.assertEqual(rnd["questions"][0]["answer"], "答复")
        # 答复不可改写
        with self.assertRaises(ConflictStateError):
            self.svc.answer_question("c1", qid, "新答复", actor="甲")


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        seed_reviewers(self.svc)

    def _closed_case(self, case_id, scores):
        self.svc.create_case(case_id, case_id.upper(), "core_components", "甲",
                             "甲公司", "5000", actor="甲")
        self.svc.submit_application(case_id, "路线", [{"name": "甲"}], {"x": 1},
                                    [], actor="甲")
        self.svc.open_review_round(case_id)
        self.svc.assign_reviewers(case_id)
        ids = [a["reviewer_id"]
               for a in self.svc.case_view(case_id)["rounds"][-1]["assignments"]]
        for rid, sc in zip(ids, scores):
            self.svc.submit_score(case_id, rid, sc)
        self.svc.close_round(case_id)
        return ids

    def _sc(self, t, m, k, c):
        return {"technology": t, "team": m, "market": k, "compliance": c}

    def test_decision_must_match_score_band(self) -> None:
        scores = [self._sc(50, 50, 50, 50)] * 3
        self._closed_case("c-lo", scores)
        with self.assertRaises(ConflictStateError):
            self.svc.make_decision("c-lo", "approved", "分数不足不能批准", actor="head")
        self.svc.make_decision("c-lo", "rejected", "均分低于60", actor="head")
        self.assertEqual(self.svc.case_view("c-lo")["status"], "rejected")

    def test_conditional_decision_requires_conditions(self) -> None:
        scores = [self._sc(65, 65, 65, 65)] * 3
        self._closed_case("c-mid", scores)
        with self.assertRaises(ValidationError):
            self.svc.make_decision("c-mid", "conditional", "需补充材料", actor="head",
                                   conditions=[])
        self.svc.make_decision("c-mid", "conditional", "补充担保后通过", actor="head",
                               conditions=["30天内追加担保函"])
        view = self.svc.case_view("c-mid")
        self.assertEqual(view["status"], "conditionally_approved")
        with self.assertRaises(ConflictStateError):
            self.svc.sign_contract("c-mid", "fund-x", [
                {"milestone_id": "m1", "name": "一", "amount": "1000",
                 "criteria": "c"}], actor="admin")
        self.svc.satisfy_conditions("c-mid", "担保函已上传", actor="admin")
        self.assertEqual(self.svc.case_view("c-mid")["status"], "approved")

    def test_decision_records_score_explanation(self) -> None:
        cid = approved_case(self.svc)
        dec = self.svc.case_view(cid)["decision"]
        self.assertIn("score_summary", dec)
        self.assertIn("dimension_avg", dec["score_summary"])
        self.assertIn("thresholds", dec)


class AppealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        seed_reviewers(self.svc)

    def test_appeal_uphold_and_reopen(self) -> None:
        cid = approved_case(self.svc, case_id="c1", code="CASE-1")
        self.svc.appeal_decision("c1", "认为技术评分存在误判", actor="陈")
        with self.assertRaises(ConflictStateError):
            self.svc.appeal_decision("c1", "重复申诉", actor="陈")
        with self.assertRaises(ValidationError):
            self.svc.rule_appeal("c1", "invalidate", "x", actor="committee")
        self.svc.rule_appeal("c1", "uphold", "申诉理由不成立", actor="committee")
        self.assertEqual(self.svc.case_view("c1")["status"], "approved")
        # 裁决后可再次申诉（针对新发现证据的场景不允许同一申诉重复裁决）
        with self.assertRaises(ConflictStateError):
            self.svc.rule_appeal("c1", "reopen", "重复裁决", actor="committee")

    def test_appeal_reopen_allows_new_round(self) -> None:
        cid = approved_case(self.svc, case_id="c2", code="CASE-2")
        self.svc.appeal_decision("c2", "程序瑕疵：评委未读补充材料", actor="陈")
        self.svc.rule_appeal("c2", "reopen", "重组评审", actor="committee")
        self.assertEqual(self.svc.case_view("c2")["status"], "in_review")
        # 原决策仍保留在历史中，可审计
        self.assertIsNotNone(self.svc.case_view("c2")["decision"])
        self.svc.open_review_round("c2")
        self.svc.assign_reviewers("c2")
        self.assertEqual(
            self.svc.case_view("c2")["rounds"][-1]["round_no"], 2)


if __name__ == "__main__":
    unittest.main()
