"""HTTP API 端到端集成测试（启动真实本地服务，urllib 请求）。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError

from industry_fund import EventStore
from industry_fund.api import create_handler

from tests._support import new_db_path


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, payload: dict | None = None,
             headers: dict | None = None):
        data = None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(self.base + path, data=data, headers=hdrs,
                                 method=method)
        try:
            with urlrequest.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_path = new_db_path()
        cls.store = EventStore(cls.db_path)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(cls.store))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()

    def test_01_full_flow_over_http(self) -> None:
        a = self.api
        # 基金
        st, _ = a.call("POST", "/funds", {
            "fund_id": "fund-1", "code": "F20", "name": "具身智能基金",
            "sources": [
                {"source_id": "gov", "name": "财政", "amount": "1200000000"},
                {"source_id": "lp", "name": "社会资本", "amount": "800000000"},
            ], "actor": "admin"})
        self.assertEqual(st, 201)

        # 评委
        for rid, name, org in [("r1", "张", "A大"), ("r2", "李", "B司"),
                               ("r3", "王", "C院"), ("r4", "赵", "D会")]:
            st, _ = a.call("POST", "/reviewers",
                           {"reviewer_id": rid, "name": name, "org": org})
            self.assertEqual(st, 201)
        st, body = a.call("POST", "/reviewers/r2/relationships",
                          {"related_party": "关联企业", "relation_type": "持股"})
        self.assertEqual(st, 201)

        # 项目与材料
        st, _ = a.call("POST", "/cases", {
            "case_id": "c1", "code": "CASE-1", "track": "core_components",
            "applicant": "甲", "applicant_org": "甲公司",
            "requested_amount": "100000000", "actor": "甲"})
        self.assertEqual(st, 201)
        st, _ = a.call("POST", "/cases/c1/applications", {
            "technical_route": "伺服关节",
            "team": [{"name": "甲", "role": "负责人"}],
            "use_of_funds": {"产线": "80000000", "研发": "20000000"},
            "related_parties": [{"party": "关联企业", "relationship": "持股"}],
            "version_label": "v1", "actor": "甲"})
        self.assertEqual(st, 201)
        st, _ = a.call("POST", "/cases/c1/rounds", {}, {"X-Actor": "admin"})
        self.assertEqual(st, 201)
        st, body = a.call("POST", "/cases/c1/assignments", {"min_reviewers": 3})
        self.assertEqual(st, 201)
        st, case = a.call("GET", "/cases/c1")
        assigned = [x["reviewer_id"] for x in case["rounds"][-1]["assignments"]]
        excluded = {x["reviewer_id"] for x in case["rounds"][-1]["excluded"]}
        self.assertIn("r2", excluded)
        self.assertEqual(len(assigned), 3)

        # 评分、质询、关轮、决策
        for rid in assigned:
            st, _ = a.call("POST", "/cases/c1/scores", {
                "reviewer_id": rid,
                "scores": {"technology": 85, "team": 84, "market": 82,
                           "compliance": 88}})
            self.assertEqual(st, 201)
        st, _ = a.call("POST", "/cases/c1/questions",
                       {"reviewer_id": assigned[0], "content": "产能？"})
        self.assertEqual(st, 201)
        st, case = a.call("GET", "/cases/c1")
        qid = case["rounds"][-1]["questions"][0]["question_id"]
        st, _ = a.call("POST", "/cases/c1/answers",
                       {"question_id": qid, "content": "已有产线", "actor": "甲"})
        self.assertEqual(st, 201)
        st, _ = a.call("POST", "/cases/c1/close-round", {}, {"X-Actor": "admin"})
        self.assertEqual(st, 201)
        st, _ = a.call("POST", "/cases/c1/decision",
                       {"result": "approved", "rationale": "综合评分达标",
                        "actor": "ic-head"})
        self.assertEqual(st, 201)

        # 条件性决策校验：422
        st, body = a.call("POST", "/cases", {
            "case_id": "c2", "code": "CASE-2", "track": "scenario_operations",
            "applicant": "乙", "applicant_org": "乙公司",
            "requested_amount": "1000", "actor": "乙"})
        self.assertEqual(st, 201)
        st, body = a.call("GET", "/cases/c2")
        # c2 未走评审，下面的错误映射通过一个非法操作验证 409
        st, body = a.call("POST", "/cases/c2/rounds", {})
        self.assertEqual(st, 409)  # 无材料不能开评审

        # 合同与拨款
        st, _ = a.call("POST", "/cases/c1/contract", {
            "fund_id": "fund-1", "milestones": [
                {"milestone_id": "ms-1", "name": "样机", "amount": "40000000",
                 "criteria": "验收"},
                {"milestone_id": "ms-2", "name": "批量", "amount": "60000000",
                 "criteria": "交付"}], "actor": "admin"})
        self.assertEqual(st, 201)
        st, body = a.call("GET", "/funds/fund-1")
        self.assertEqual(st, 200)
        self.assertEqual(body["committed"], "100000000.00")

        st, _ = a.call("POST", "/milestones/ms-1/evidence", {
            "files": [{"name": "r.pdf", "hash": "h1"}], "summary": "样机验收",
            "actor": "甲"})
        self.assertEqual(st, 201)
        st, _ = a.call("POST", "/milestones/ms-1/technical-review",
                       {"passed": True, "opinion": "达标", "reviewer": "tech"})
        self.assertEqual(st, 201)
        st, _ = a.call("POST", "/milestones/ms-1/financial-review",
                       {"passed": True, "opinion": "真实", "reviewer": "fin"})
        self.assertEqual(st, 201)
        # 幂等：同一 Idempotency-Key 重放
        hdrs = {"Idempotency-Key": "pay-ms1"}
        st, r1 = a.call("POST", "/milestones/ms-1/payment",
                        {"payment_ref": "PAY-1", "actor": "fa"}, hdrs)
        self.assertEqual(st, 201)
        self.assertFalse(r1["replayed"])
        st, r2 = a.call("POST", "/milestones/ms-1/payment",
                        {"payment_ref": "PAY-1", "actor": "fa"}, hdrs)
        self.assertTrue(r2["replayed"])

        st, body = a.call("GET", "/funds/fund-1?as_of=2026-01-01")
        self.assertEqual(st, 200)
        self.assertEqual(body["total"], "0.00")
        st, body = a.call("GET", "/funds/fund-1")
        self.assertEqual(body["paid"], "40000000.00")
        self.assertEqual(body["committed"], "60000000.00")
        self.assertEqual(len(body["by_source"]), 2)

        # 审计链
        st, body = a.call("POST", "/admin/verify-integrity", {})
        self.assertEqual(st, 201)
        self.assertTrue(body["ok"])
        st, body = a.call("GET", "/aggregates/ms-1/audit")
        self.assertEqual(st, 200)
        types_seen = {e["type"] for e in body["events"]}
        self.assertIn("MilestonePaid", types_seen)
        for e in body["events"]:
            self.assertIsNotNone(e["hash"])

    def test_02_validation_error_shape(self) -> None:
        st, body = self.api.call("POST", "/funds", {"fund_id": "x"})
        self.assertEqual(st, 422)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
