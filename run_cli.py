"""端到端冒烟入口：在临时数据库中演示一次完整的评审与里程碑拨款。

    python run_cli.py
"""

import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from industry_fund import EventStore, FundService, InvestmentCase, to_yuan


def main() -> None:
    # 保留基础契约演示
    item = InvestmentCase(
        **{"case_code": "case-code-001", "applicant": "applicant-001",
           "round_name": "round-name-001", "state": "draft"})
    print("基础契约:", json.dumps(
        {"item": asdict(item), "fingerprint": item.fingerprint()},
        ensure_ascii=False))

    db = tempfile.mktemp(suffix=".db")
    store = EventStore(db)
    svc = FundService(store)

    svc.establish_fund("fund-1", "F20", "具身智能产业基金", [
        {"source_id": "gov", "name": "市财政", "amount": "1200000000",
         "kind": "government"},
        {"source_id": "lp", "name": "社会资本", "amount": "800000000",
         "kind": "lp"},
    ], actor="admin")

    for rid, name, org in [("rv-a", "张", "北理工"), ("rv-b", "李", "中投"),
                           ("rv-c", "王", "产研院"), ("rv-d", "赵", "机器人协会")]:
        svc.register_reviewer(rid, name, org)
    svc.declare_relationship("rv-b", "智元公司", "持股")

    svc.create_case("case-1", "CASE-001", "university_transfer",
                    "陈教授", "清华", "100000000", actor="陈教授")
    svc.submit_application(
        "case-1", "人形机器人灵巧手技术路线",
        [{"name": "陈教授", "role": "PI"}],
        {"研发": "60000000", "设备": "40000000"},
        [{"party": "智元公司", "relationship": "持股企业"}], actor="陈教授")
    svc.open_review_round("case-1")
    svc.assign_reviewers("case-1")
    assigned = [a["reviewer_id"]
                for a in svc.case_view("case-1")["rounds"][-1]["assignments"]]
    for rid in assigned:
        svc.submit_score("case-1", rid,
                         {"technology": 85, "team": 84, "market": 82,
                          "compliance": 88})
    svc.ask_question("case-1", assigned[0], "专利归属？")
    qid = svc.case_view("case-1")["rounds"][-1]["questions"][0]["question_id"]
    svc.answer_question("case-1", qid, "高校独占许可", actor="陈教授")
    svc.close_round("case-1")
    svc.make_decision("case-1", "approved", "技术领先", "ic-head")
    svc.sign_contract("case-1", "fund-1", [
        {"milestone_id": "ms-1", "name": "原型机", "amount": "30000000",
         "criteria": "样机验收"},
        {"milestone_id": "ms-2", "name": "中试", "amount": "30000000",
         "criteria": "良率90%"},
        {"milestone_id": "ms-3", "name": "量产", "amount": "40000000",
         "criteria": "千台交付"},
    ], actor="admin")

    svc.submit_evidence("ms-1", [{"name": "验收报告.pdf", "hash": "abc"}],
                        "样机通过验收", actor="陈教授")
    svc.review_technical("ms-1", True, "技术达标", "tech")
    svc.review_financial("ms-1", True, "费用真实", "fin")
    svc.release_payment("ms-1", "PAY-2026-0001", actor="fin-admin",
                        idem_key="demo-pay-ms1")

    pos = svc.fund_position("fund-1")
    print("资金头寸:", json.dumps({
        "总额": to_yuan(pos["total_cents"]),
        "可用": to_yuan(pos["available_cents"]),
        "已承诺": to_yuan(pos["committed_cents"]),
        "冻结": to_yuan(pos["frozen_cents"]),
        "已付": to_yuan(pos["paid_cents"]),
        "守恒": (pos["available_cents"] + pos["committed_cents"]
                 + pos["frozen_cents"] + pos["paid_cents"]) == pos["total_cents"],
    }, ensure_ascii=False))

    store.verify_chain()
    print("审计哈希链: 校验通过")
    store.close()
    os.unlink(db)


if __name__ == "__main__":
    main()
