"""测试公共构造工具。"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from industry_fund import EventStore, FundService


def new_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


def make_service(path: str | None = None, clock=None) -> tuple[FundService, EventStore, str]:
    path = path or new_db_path()
    store = EventStore(path, clock=clock or (lambda: datetime.now(timezone.utc)))
    return FundService(store), store, path


REVIEWERS = [
    ("rv-a", "张", "北理工"),
    ("rv-b", "李", "中投"),
    ("rv-c", "王", "产研院"),
    ("rv-d", "赵", "机器人协会"),
]


def seed_fund(svc: FundService, total_sources=None) -> str:
    sources = total_sources or [
        {"source_id": "gov", "name": "市财政", "amount": "1200000000", "kind": "government"},
        {"source_id": "lp", "name": "社会资本", "amount": "800000000", "kind": "lp"},
    ]
    svc.establish_fund("fund-1", "F20", "具身智能产业基金", sources, actor="admin")
    return "fund-1"


def seed_reviewers(svc: FundService, relations=None) -> None:
    for rid, name, org in REVIEWERS:
        svc.register_reviewer(rid, name, org)
    for rid, party, kind in relations or [("rv-b", "智元公司", "持股")]:
        svc.declare_relationship(rid, party, kind)


def approved_case(svc: FundService, case_id="case-1", code="CASE-001",
                  track="university_transfer", applicant="陈教授", org="清华",
                  requested="100000000", parties=None, scores=None,
                  min_reviewers=3) -> str:
    """走完 创建->材料->评审->决策(approved) 的完整流程。"""
    parties = parties if parties is not None else [
        {"party": "智元公司", "relationship": "持股企业"}]
    svc.create_case(case_id, code, track, applicant, org, requested, actor=applicant)
    svc.submit_application(
        case_id, "技术路线说明", [{"name": applicant, "role": "PI"}],
        {"研发": "60%", "设备": "40%"}, parties, actor=applicant)
    svc.open_review_round(case_id)
    result = svc.assign_reviewers(case_id, min_reviewers=min_reviewers)
    assigned = result  # 返回的是事件摘要，需要读视图
    view = svc.case_view(case_id)
    rnd = view["rounds"][-1]
    assigned_ids = [a["reviewer_id"] for a in rnd["assignments"]]
    default_scores = [
        {"technology": 88, "team": 86, "market": 80, "compliance": 90},
        {"technology": 82, "team": 84, "market": 79, "compliance": 88},
        {"technology": 90, "team": 85, "market": 82, "compliance": 91},
    ]
    scores = scores or default_scores
    for rid, sc in zip(assigned_ids, scores):
        svc.submit_score(case_id, rid, sc)
    svc.ask_question(case_id, assigned_ids[0], "是否有知识产权纠纷？")
    qid = svc.case_view(case_id)["rounds"][-1]["questions"][0]["question_id"]
    svc.answer_question(case_id, qid, "无纠纷", actor=applicant)
    svc.close_round(case_id)
    svc.make_decision(case_id, "approved", "技术领先且无重大风险", actor="ic-head")
    return case_id


def contracted_case(svc: FundService, milestones=None, **kw) -> str:
    fund_id = seed_fund(svc)
    seed_reviewers(svc)
    case_id = approved_case(svc, **kw)
    milestones = milestones or [
        {"milestone_id": "ms-1", "name": "原型机", "amount": "30000000", "criteria": "验收"},
        {"milestone_id": "ms-2", "name": "中试", "amount": "30000000", "criteria": "良率90%"},
        {"milestone_id": "ms-3", "name": "量产", "amount": "40000000", "criteria": "千台"},
    ]
    svc.sign_contract(case_id, fund_id, milestones, actor="admin")
    return case_id
