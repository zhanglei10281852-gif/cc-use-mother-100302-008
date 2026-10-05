"""领域模型：事件类型、聚合归约（reducer）与业务规则。

聚合：
- Fund          基金预算（承诺/冻结/已付/可用的权威来源由 ledger 投影派生）
- ReviewerPool  评委名册与回避关系申报
- Case          项目：材料版本、评审轮次、评分质询、决策申诉、合同
- Milestone     里程碑：证据、技术/财务复核、冻结、支付、撤回、终止
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ConflictStateError, NotFoundError, ValidationError

# ---------------- 事件类型 ----------------

# Fund
FUND_ESTABLISHED = "FundEstablished"
FUNDS_ALLOCATED = "FundsAllocated"
ALLOCATION_RELEASED = "AllocationReleased"
MILESTONE_FROZEN = "MilestoneFrozen"
MILESTONE_UNFROZEN = "MilestoneUnfrozen"
PAYMENT_RELEASED = "PaymentReleased"
PAYMENT_CLAWED_BACK = "PaymentClawedBack"

# ReviewerPool
REVIEWER_REGISTERED = "ReviewerRegistered"
REVIEWER_DEACTIVATED = "ReviewerDeactivated"
RELATIONSHIP_DECLARED = "RelationshipDeclared"

# Case
CASE_CREATED = "CaseCreated"
APPLICATION_SUBMITTED = "ApplicationSubmitted"
REVIEW_ROUND_OPENED = "ReviewRoundOpened"
REVIEWERS_ASSIGNED = "ReviewersAssigned"
SCORE_SUBMITTED = "ScoreSubmitted"
QUESTION_RAISED = "QuestionRaised"
QUESTION_ANSWERED = "QuestionAnswered"
REVIEW_CLOSED = "ReviewClosed"
ROUND_SUPERSEDED = "RoundSuperseded"
DECISION_MADE = "DecisionMade"
CONDITIONS_SATISFIED = "ConditionsSatisfied"
DECISION_APPEALED = "DecisionAppealed"
APPEAL_RULED = "AppealRuled"
CONTRACT_SIGNED = "ContractSigned"
MILESTONE_DEFINED = "MilestoneDefined"
FAILURE_FLAG_RAISED = "FailureFlagRaised"
FAILURE_WAIVED = "FailureWaived"
CASE_TERMINATED = "CaseTerminated"
CASE_COMPLETED = "CaseCompleted"

# Milestone
EVIDENCE_SUBMITTED = "EvidenceSubmitted"
TECHNICAL_REVIEW_PASSED = "TechnicalReviewPassed"
TECHNICAL_REVIEW_FAILED = "TechnicalReviewFailed"
FINANCIAL_REVIEW_PASSED = "FinancialReviewPassed"
FINANCIAL_REVIEW_FAILED = "FinancialReviewFailed"
MILESTONE_PAID = "MilestonePaid"
MILESTONE_CLAWED_BACK = "MilestoneClawedBack"
MILESTONE_CANCELLED = "MilestoneCancelled"

TRACKS = ("university_transfer", "core_components", "scenario_operations")

# 案例状态
ST_CREATED = "created"
ST_IN_REVIEW = "in_review"
ST_APPROVED = "approved"
ST_CONDITIONAL = "conditionally_approved"
ST_REJECTED = "rejected"
ST_CONTRACTED = "contracted"
ST_TERMINATED = "terminated"
ST_COMPLETED = "completed"

# 评审轮次状态
R_OPEN = "open"
R_CLOSED = "closed"
R_SUPERSEDED = "superseded"

# 里程碑状态
M_PENDING = "pending"
M_EVIDENCE = "evidence_submitted"
M_TECH_PASSED = "technical_passed"
M_REVIEWED = "reviewed"  # 技术+财务均通过，已冻结
M_FAILED = "failed"
M_PAID = "paid"
M_CLAWED_BACK = "clawed_back"
M_CANCELLED = "cancelled"

SCORE_DIMENSIONS = ("technology", "team", "market", "compliance")
DEFAULT_APPROVE_LINE = 75.0
DEFAULT_CONDITIONAL_LINE = 60.0


def require(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} 不能为空")
    return value.strip()


# ---------------- 状态结构 ----------------


@dataclass
class RoundState:
    round_no: int
    status: str = R_OPEN
    assignments: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    assignment_note: str = ""
    scores: dict[str, dict] = field(default_factory=dict)
    abstentions: dict[str, str] = field(default_factory=dict)
    questions: list[dict] = field(default_factory=list)
    opened_at: str | None = None
    closed_at: str | None = None

    @property
    def reviewer_ids(self) -> set[str]:
        return {a["reviewer_id"] for a in self.assignments}

    @property
    def open_questions(self) -> list[dict]:
        return [q for q in self.questions if q.get("answer") is None]


@dataclass
class CaseState:
    case_id: str
    code: str = ""
    track: str = ""
    applicant: str = ""
    applicant_org: str = ""
    requested_cents: int = 0
    fund_id: str | None = None
    status: str = ""
    versions: list[dict] = field(default_factory=list)
    rounds: dict[int, RoundState] = field(default_factory=dict)
    current_round_no: int = 0
    decision: dict | None = None
    conditions_satisfied: bool = False
    appeal: dict | None = None
    contract: dict | None = None
    milestone_ids: list[str] = field(default_factory=list)
    failure_flag: dict | None = None
    failure_waiver: dict | None = None
    termination: dict | None = None
    revision: int = 0

    @property
    def version(self) -> int:
        return self.revision

    @property
    def current_version(self) -> dict | None:
        return self.versions[-1] if self.versions else None

    def round(self, round_no: int | None = None) -> RoundState:
        no = self.current_round_no if round_no is None else round_no
        r = self.rounds.get(no)
        if r is None:
            raise ConflictStateError(f"项目 {self.code} 不存在第 {no} 轮评审")
        return r


@dataclass
class MilestoneState:
    milestone_id: str
    case_id: str
    seq: int = 0
    name: str = ""
    description: str = ""
    due_date: str = ""
    amount_cents: int = 0
    criteria: str = ""
    fund_id: str = ""
    status: str = M_PENDING
    evidence: list[dict] = field(default_factory=list)
    technical: dict | None = None
    financial: dict | None = None
    frozen_at: str | None = None
    payment_ref: str | None = None
    paid_at: str | None = None
    clawback: dict | None = None
    revision: int = 0

    @property
    def version(self) -> int:
        return self.revision


@dataclass
class FundState:
    fund_id: str
    code: str = ""
    name: str = ""
    total_cents: int = 0
    sources: list[dict] = field(default_factory=list)
    established: bool = False
    revision: int = 0

    @property
    def version(self) -> int:
        return self.revision


@dataclass
class PoolState:
    reviewers: dict[str, dict] = field(default_factory=dict)
    revision: int = 0

    @property
    def version(self) -> int:
        return self.revision


POOL_ID = "reviewer-pool"
AGG_CASE = "Case"
AGG_MILESTONE = "Milestone"
AGG_FUND = "Fund"
AGG_POOL = "ReviewerPool"


# ---------------- 归约器 ----------------


def apply_case(state: CaseState | None, event_type: str, p: dict) -> CaseState:
    if state is None:
        if event_type != CASE_CREATED:
            raise NotFoundError(f"未知项目事件: {event_type}")
        s = CaseState(
            case_id=p["case_id"],
            code=p["code"],
            track=p["track"],
            applicant=p["applicant"],
            applicant_org=p.get("applicant_org", ""),
            requested_cents=p["requested_cents"],
            status=ST_CREATED,
        )
        s.revision = 1
        return s

    s = state
    if event_type == APPLICATION_SUBMITTED:
        s.versions.append(p["snapshot"])
    elif event_type == REVIEW_ROUND_OPENED:
        r = RoundState(round_no=p["round_no"], opened_at=p.get("at"))
        s.rounds[p["round_no"]] = r
        s.current_round_no = p["round_no"]
        s.status = ST_IN_REVIEW
    elif event_type == REVIEWERS_ASSIGNED:
        r = s.round(p["round_no"])
        r.assignments = p["assignments"]
        r.excluded = p["excluded"]
        r.assignment_note = p.get("note", "")
    elif event_type == SCORE_SUBMITTED:
        r = s.round(p["round_no"])
        if p.get("abstain"):
            r.abstentions[p["reviewer_id"]] = p.get("reason", "")
        else:
            r.scores[p["reviewer_id"]] = p
    elif event_type == QUESTION_RAISED:
        s.round(p["round_no"]).questions.append(p)
    elif event_type == QUESTION_ANSWERED:
        for q in s.round(p["round_no"]).questions:
            if q["question_id"] == p["question_id"]:
                q["answer"] = p["content"]
                q["answered_at"] = p["at"]
                break
    elif event_type == REVIEW_CLOSED:
        r = s.round(p["round_no"])
        r.status = R_CLOSED
        r.closed_at = p.get("at")
    elif event_type == ROUND_SUPERSEDED:
        s.round(p["round_no"]).status = R_SUPERSEDED
    elif event_type == DECISION_MADE:
        s.decision = p
        s.appeal = None
        s.conditions_satisfied = False
        s.status = (
            ST_APPROVED if p["result"] == "approved"
            else ST_CONDITIONAL if p["result"] == "conditional"
            else ST_REJECTED
        )
    elif event_type == CONDITIONS_SATISFIED:
        s.conditions_satisfied = True
        s.status = ST_APPROVED
    elif event_type == DECISION_APPEALED:
        s.appeal = p
    elif event_type == APPEAL_RULED:
        s.appeal = {**(s.appeal or {}), "ruling": p}
        if p["ruling"] == "reopen":
            s.status = ST_IN_REVIEW
    elif event_type == CONTRACT_SIGNED:
        s.contract = p
        s.fund_id = p["fund_id"]
        s.milestone_ids = list(p["milestone_ids"])
        s.status = ST_CONTRACTED
    elif event_type == FAILURE_FLAG_RAISED:
        s.failure_flag = p
    elif event_type == FAILURE_WAIVED:
        s.failure_waiver = p
        s.failure_flag = None
    elif event_type == CASE_TERMINATED:
        s.termination = p
        s.status = ST_TERMINATED
    else:
        raise NotFoundError(f"项目聚合不接受事件 {event_type}")
    s.revision += 1
    return s


def apply_milestone(state: MilestoneState | None, event_type: str, p: dict) -> MilestoneState:
    if state is None:
        if event_type != MILESTONE_DEFINED:
            raise NotFoundError(f"未知里程碑事件: {event_type}")
        s = MilestoneState(
            milestone_id=p["milestone_id"],
            case_id=p["case_id"],
            seq=p["seq"],
            name=p["name"],
            description=p.get("description", ""),
            due_date=p.get("due_date", ""),
            amount_cents=p["amount_cents"],
            criteria=p.get("criteria", ""),
            fund_id=p.get("fund_id", ""),
        )
        s.revision = 1
        return s

    s = state
    if event_type == EVIDENCE_SUBMITTED:
        s.evidence.append(p)
        if s.status == M_PENDING:
            s.status = M_EVIDENCE
    elif event_type == TECHNICAL_REVIEW_PASSED:
        s.technical = p
        if s.status == M_EVIDENCE:
            s.status = M_TECH_PASSED
    elif event_type == TECHNICAL_REVIEW_FAILED:
        s.technical = p
        s.status = M_FAILED
    elif event_type == FINANCIAL_REVIEW_PASSED:
        s.financial = p
        if s.technical is not None and s.technical.get("passed"):
            s.status = M_REVIEWED
    elif event_type == FINANCIAL_REVIEW_FAILED:
        s.financial = p
        s.status = M_FAILED
    elif event_type == MILESTONE_FROZEN:
        s.frozen_at = p["at"]
        s.status = M_REVIEWED
    elif event_type == MILESTONE_UNFROZEN:
        s.frozen_at = None
        s.status = M_REVIEWED
    elif event_type == MILESTONE_PAID:
        s.payment_ref = p["payment_ref"]
        s.paid_at = p["at"]
        s.status = M_PAID
    elif event_type == MILESTONE_CLAWED_BACK:
        s.clawback = p
        s.status = M_CLAWED_BACK
    elif event_type == MILESTONE_CANCELLED:
        s.status = M_CANCELLED
    else:
        raise NotFoundError(f"里程碑聚合不接受事件 {event_type}")
    s.revision += 1
    return s  # type: ignore[return-value]


def apply_fund(state: FundState | None, event_type: str, p: dict) -> FundState:
    if state is None:
        if event_type != FUND_ESTABLISHED:
            raise NotFoundError(f"未知基金事件: {event_type}")
        s = FundState(
            fund_id=p["fund_id"], code=p["code"], name=p["name"],
            total_cents=p["total_cents"], sources=p["sources"], established=True,
        )
        s.revision = 1
        return s
    # 资金事件只改变预算投影（ledger），基金聚合本身仅记录版本推进
    state.revision += 1
    return state


def apply_pool(state: PoolState | None, event_type: str, p: dict) -> PoolState:
    s = state or PoolState()
    if state is None and event_type != REVIEWER_REGISTERED:
        raise NotFoundError(f"评委名册尚未初始化: {event_type}")
    if event_type == REVIEWER_REGISTERED:
        if p["reviewer_id"] in s.reviewers:
            raise ConflictStateError(f"评委 {p['reviewer_id']} 已注册")
        s.reviewers[p["reviewer_id"]] = {
            "reviewer_id": p["reviewer_id"],
            "name": p["name"],
            "org": p.get("org", ""),
            "expertise": list(p.get("expertise", [])),
            "active": True,
            "relations": [],
        }
    elif event_type == REVIEWER_DEACTIVATED:
        s.reviewers[p["reviewer_id"]]["active"] = False
    elif event_type == RELATIONSHIP_DECLARED:
        s.reviewers[p["reviewer_id"]]["relations"].append(p)
    else:
        raise NotFoundError(f"评委名册不接受事件 {event_type}")
    s.revision += 1
    return s


def fold(events: list, agg: str) -> Any:
    """从事件流重建聚合状态。"""
    state: Any = None
    for e in events:
        if agg == AGG_CASE:
            state = apply_case(state, e.event_type, e.payload)
        elif agg == AGG_MILESTONE:
            state = apply_milestone(state, e.event_type, e.payload)
        elif agg == AGG_FUND:
            state = apply_fund(state, e.event_type, e.payload)
        elif agg == AGG_POOL:
            state = apply_pool(state, e.event_type, e.payload)
        else:
            raise NotFoundError(f"未知聚合类型 {agg}")
    return state
