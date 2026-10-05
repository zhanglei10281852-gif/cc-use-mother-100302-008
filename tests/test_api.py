"""HTTP API 端到端测试（标准库 http.client，无需第三方依赖）。"""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from urllib.parse import quote

from industry_fund.api import create_server

BASE_APP = {
    "project_name": "具身智能控制器项目",
    "kind": "component",
    "tech_route": "自研力控芯片加软件栈，两年迭代三代",
    "team": [{"name": "陈工", "org": "流片科技", "role": "CTO"}],
    "fund_usage": [
        {"purpose": "研发", "amount": "300000000"},
        {"purpose": "产线", "amount": "200000000"},
    ],
    "related_parties": [
        {"name": "流片科技", "relation": "申请主体", "party_type": "org"},
    ],
    "requested_amount": "500000000",
}
DIMENSIONS = {"tech": 85, "team": 80, "usage": 78, "market": 82, "risk": 75}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = None
        hdr = dict(headers or {})
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdr["Content-Type"] = "application/json"
        conn.request(method, path, data, hdr)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        payload = json.loads(raw) if raw else {}
        return resp.status, payload

    def test_full_journey_over_http(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)

        status, _ = self.request("POST", "/fund/sources",
                                 {"source": "gov", "name": "引导基金", "amount": "800000000"})
        self.assertEqual(status, 200)
        for rid, name in [("e1", "评委一"), ("e2", "评委二"), ("e3", "评委三")]:
            status, _ = self.request("POST", "/reviewers",
                                     {"reviewer_id": rid, "name": name,
                                      "expertise": ["控制"], "affiliations": [],
                                      "related_party_keys": []})
            self.assertEqual(status, 200)

        code = "api-case-1"
        self.assertEqual(self.request("POST", "/cases", {
            "case_code": code, "applicant": "流片科技", "round_name": "2026Q4"})[0], 200)
        self.assertEqual(self.request("POST", f"/cases/{code}/applications", BASE_APP)[0], 200)
        self.assertEqual(self.request("POST", f"/cases/{code}/lock", {})[0], 200)
        status, body = self.request("POST", f"/cases/{code}/assignments",
                                    {"reviewer_ids": ["e1", "e2", "e3"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["eligible_reviewer_ids"]), 3)

        for rid in ["e1", "e2", "e3"]:
            self.assertEqual(self.request("POST", f"/cases/{code}/scores/{rid}", {
                "dimensions": DIMENSIONS, "rationale": f"{rid} 看好"} )[0], 200)
            self.assertEqual(self.request("POST", f"/cases/{code}/ballots/{rid}", {
                "vote": "approve", "comment": "同意"})[0], 200)

        status, decision = self.request("POST", f"/cases/{code}/decision",
                                        {"decided_by": "ic", "rationale": "通过"})
        self.assertEqual(status, 200)
        self.assertEqual(decision["outcome"], "approve")

        milestones = [
            {"seq": 1, "name": "流片", "criteria": "样片点亮", "amount": "300000000"},
            {"seq": 2, "name": "量产", "criteria": "月产一万颗", "amount": "200000000"},
        ]
        self.assertEqual(self.request("POST", f"/cases/{code}/contract",
                                      {"milestones": milestones, "signed_by": "ic"})[0], 200)
        self.assertEqual(self.request("POST", f"/cases/{code}/milestones/1/evidence",
                                      {"evidence_ref": "oss://ev1", "submitted_by": "pm"})[0], 200)
        self.assertEqual(self.request("POST", f"/cases/{code}/milestones/1/tech-review",
                                      {"approved": True, "review_ref": "tr1",
                                       "reviewer": "cto"})[0], 200)
        self.assertEqual(self.request("POST", f"/cases/{code}/milestones/1/finance-review",
                                      {"approved": True, "review_ref": "fr1",
                                       "reviewer": "cfo"})[0], 200)

        headers = {"Idempotency-Key": "http-pay-1"}
        status, paid = self.request("POST", f"/cases/{code}/milestones/1/disbursements",
                                    {"idempotency_key": "biz-pay-1", "requested_by": "t",
                                     "reference": "REF"}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(paid["amount_cents"], 300_000_000_00)
        # 整个 HTTP 请求重试一次，返回同一结果且不重复支付。
        status, replay = self.request("POST", f"/cases/{code}/milestones/1/disbursements",
                                      {"idempotency_key": "biz-pay-1", "requested_by": "t",
                                       "reference": "REF"}, headers)
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])

        status, snap = self.request("GET", "/fund/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(snap["paid"]["cents"], 300_000_000_00)
        self.assertEqual(snap["committed"]["cents"], 200_000_000_00)
        self.assertEqual(snap["total_fund"]["cents"], 800_000_000_00)
        self.assertIn("gov", snap["by_source"])

        status, audit = self.request("GET", "/audit")
        self.assertEqual(status, 200)
        self.assertTrue(audit["verification"]["audit_chain_intact"])
        self.assertTrue(audit["verification"]["ledger_conserved"])
        self.assertGreater(len(audit["records"]), 10)

    def test_conflict_reviewer_blocked_over_http(self) -> None:
        self.request("POST", "/reviewers", {
            "reviewer_id": "conflicted", "name": "评委冲突", "expertise": [],
            "affiliations": ["流片科技"], "related_party_keys": []})
        code = "api-case-2"
        self.request("POST", "/cases", {"case_code": code, "applicant": "流片科技",
                                        "round_name": "r"})
        self.request("POST", f"/cases/{code}/applications", BASE_APP)
        self.request("POST", f"/cases/{code}/lock", {})
        self.request("POST", "/reviewers", {
            "reviewer_id": "ok1", "name": "甲", "expertise": [], "affiliations": [],
            "related_party_keys": []})
        self.request("POST", "/reviewers", {
            "reviewer_id": "ok2", "name": "乙", "expertise": [], "affiliations": [],
            "related_party_keys": []})
        self.request("POST", "/reviewers", {
            "reviewer_id": "ok3", "name": "丙", "expertise": [], "affiliations": [],
            "related_party_keys": []})
        status, body = self.request("POST", f"/cases/{code}/assignments",
                                    {"reviewer_ids": ["conflicted", "ok1", "ok2", "ok3"]})
        self.assertEqual(status, 200)
        by_id = {a["reviewer_id"]: a for a in body["assignments"]}
        self.assertFalse(by_id["conflicted"]["eligible"])
        # 冲突评委打分被拒。
        status, err = self.request("POST", f"/cases/{code}/scores/conflicted", {
            "dimensions": DIMENSIONS, "rationale": "想打分"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "state")


if __name__ == "__main__":
    unittest.main()
