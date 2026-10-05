"""命令行冒烟入口：跑通一笔从评审到拨款的最小完整流程并打印资金快照。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from industry_fund import Clock, FundService, Repository
from datetime import datetime, timezone


def main() -> None:
    svc = FundService(Repository(":memory:"),
                      Clock(datetime(2026, 10, 5, tzinfo=timezone.utc)))
    svc.add_fund_source("gov", "政府引导基金", "1500000000")
    for rid, name in [("r1", "张评委"), ("r2", "赵评委"), ("r3", "钱评委")]:
        svc.register_reviewer(rid, name, ["机器人"], [], [])
    svc.create_case("case-demo", "自旋机器人公司", "2026秋季")
    svc.submit_application("case-demo", {
        "project_name": "灵巧手产业化",
        "kind": "tech_transfer",
        "tech_route": "触觉传感三代路线",
        "team": [{"name": "王教授", "org": "同源大学", "role": "负责人"}],
        "fund_usage": [{"purpose": "中试线", "amount": "1000000000"}],
        "related_parties": [{"name": "同源大学", "relation": "依托单位",
                             "party_type": "org"}],
        "requested_amount": "1000000000",
    }, "applicant")
    svc.lock_for_review("case-demo")
    assigned = svc.assign_reviewers("case-demo", ["r1", "r2", "r3"])
    dims = {"tech": 90, "team": 85, "usage": 80, "risk": 75}
    for rid in assigned["eligible_reviewer_ids"]:
        svc.submit_score("case-demo", rid, dims, "技术路线清晰")
        svc.cast_ballot("case-demo", rid, "approve", "同意")
    svc.decide("case-demo", "ic", rationale="通过")
    svc.sign_contract("case-demo", [
        {"seq": 1, "name": "中试验收", "criteria": "良率>=95%",
         "amount": "600000000"},
        {"seq": 2, "name": "量产", "criteria": "出货1000套",
         "amount": "400000000"},
    ], "ic")
    svc.submit_evidence("case-demo", 1, "s3://ev/1", "pm")
    svc.tech_review("case-demo", 1, True, "tr-1", "cto")
    svc.finance_review("case-demo", 1, True, "fr-1", "cfo")
    paid = svc.disburse("case-demo", 1, "demo-key-1", "treasury")
    snap = svc.fund_snapshot()
    verify = svc.verify()
    print(json.dumps({
        "eligible_reviewers": assigned["eligible_reviewer_ids"],
        "paid": paid["amount_cents"],
        "fund": {k: snap[k] for k in ("committed", "paid", "frozen", "available")},
        "verification": verify,
        "audit_records": len(svc.audit_log()),
    }, ensure_ascii=False, sort_keys=True, indent=2, default=str))


if __name__ == "__main__":
    main()
