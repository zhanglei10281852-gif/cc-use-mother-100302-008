"""领域模型：申请版本、回避关系、评审、决策、合同里程碑、拨款与台账。

所有写操作产出的对象都是冻结数据类，业务状态只追加不改写；
金额统一使用 ``Money``（整数分）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import Money, canonical_fingerprint


# ---------------------------------------------------------------- 申请与回避

class CaseState(StrEnum):
    DRAFT = "draft"                # 材料编辑中
    IN_REVIEW = "in_review"        # 已锁定送评
    DECIDED = "decided"            # 投资决策已出（通过/否决/有条件通过）
    CONTRACTED = "contracted"      # 已签里程碑合同
    TERMINATED = "terminated"      # 终止（含里程碑失败）
    ARCHIVED = "archived"          # 否决/归档


class ApplicationKind(StrEnum):
    TECH_TRANSFER = "tech_transfer"      # 高校成果转化
    COMPONENT = "component"              # 核心零部件
    SCENARIO_OPS = "scenario_ops"        # 场景运营


@dataclass(frozen=True, slots=True)
class RelatedParty:
    """关联方：用于评委回避比对。"""

    name: str
    relation: str                       # 例如 任职/持股/近亲属/近期共同任职
    party_type: str = "org"             # org | person
    detail: str = ""

    def key(self) -> str:
        return f"{self.party_type}:{self.name.strip()}"


@dataclass(frozen=True, slots=True)
class ApplicationContent:
    """某次提交的完整材料正文。"""

    project_name: str
    kind: str
    tech_route: str                     # 技术路线
    team: list[dict[str, Any]]
    fund_usage: list[dict[str, Any]]    # 资金用途，每项含用途与金额(元)
    related_parties: list[RelatedParty]
    requested_amount: Money

    def fingerprint(self) -> str:
        return canonical_fingerprint({
            "project_name": self.project_name,
            "kind": self.kind,
            "tech_route": self.tech_route,
            "team": self.team,
            "fund_usage": self.fund_usage,
            "related_parties": [asdict(p) for p in self.related_parties],
            "requested_amount": self.requested_amount.to_dict(),
        })


@dataclass(frozen=True, slots=True)
class ApplicationVersion:
    """材料的不可变版本；同内容重复提交得到同一版本号（幂等）。"""

    case_code: str
    version: int
    content_fingerprint: str
    content: ApplicationContent
    submitted_by: str
    submitted_at: str
    parent_version: int | None


@dataclass(frozen=True, slots=True)
class Reviewer:
    reviewer_id: str
    name: str
    expertise: list[str]               # 技术领域标签
    affiliations: list[str]            # 任职机构（组织键）
    related_party_keys: list[str]      # 个人利益关系键


# ---------------------------------------------------------------- 评审分配与打分

class AssignmentStatus(StrEnum):
    ASSIGNED = "assigned"
    RECUSED = "recused"               # 事后发现回避，该票作废
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class Assignment:
    """评委-案件分配记录及系统给出的回避解释。"""

    case_code: str
    reviewer_id: str
    assigned_at: str
    conflict_checks: list[dict[str, Any]]   # 每条关联关系的比对结果
    eligible: bool
    reason: str
    status: str = AssignmentStatus.ASSIGNED


@dataclass(frozen=True, slots=True)
class ScoreCard:
    """可解释评分：维度分 + 评分理由。"""

    case_code: str
    reviewer_id: str
    dimensions: dict[str, int]        # 维度 -> 0..100
    rationale: str
    scored_at: str

    def weighted_total(self, weights: dict[str, float]) -> float:
        total = 0.0
        for dim, score in self.dimensions.items():
            total += score * weights.get(dim, 0.0)
        return round(total, 4)


@dataclass(frozen=True, slots=True)
class Question:
    """质询：评委提出、申请方答复，均追加留痕。"""

    question_id: str
    case_code: str
    reviewer_id: str
    asked_at: str
    content: str
    response: str | None = None
    responded_at: str | None = None
    response_version: int | None = None   # 答复所依据的材料版本


class DecisionOutcome(StrEnum):
    APPROVE = "approve"
    CONDITIONAL = "conditional"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class Ballot:
    """并发安全的投票：带版本令牌，计票时可追溯每张票。"""

    case_code: str
    reviewer_id: str
    vote: str                          # approve | conditional | reject
    comment: str
    ballot_version: int                # 对应评审轮次版本
    cast_at: str


@dataclass(frozen=True, slots=True)
class Condition:
    """条件性通过的放款前置条件。"""

    condition_id: str
    description: str
    milestone_seq: int | None = None   # 可绑定到具体里程碑


@dataclass(frozen=True, slots=True)
class Decision:
    case_code: str
    outcome: str
    decided_at: str
    decided_by: str
    score_breakdown: dict[str, Any]    # 可解释的分数构成
    ballots: list[Ballot]
    conditions: list[Condition]
    approved_amount: Money
    rationale: str
    decision_version: int


# ---------------------------------------------------------------- 申诉

class AppealState(StrEnum):
    FILED = "filed"
    UPHELD = "upheld"          # 申诉成立，决策撤销，需重新评审
    REJECTED = "rejected"      # 申诉驳回，原决策有效


@dataclass(frozen=True, slots=True)
class Appeal:
    appeal_id: str
    case_code: str
    against_decision_version: int
    filed_by: str
    filed_at: str
    grounds: str
    state: str = AppealState.FILED
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    ruling: str | None = None


# ---------------------------------------------------------------- 合同与里程碑

class MilestoneStatus(StrEnum):
    PLANNED = "planned"
    EVIDENCE_SUBMITTED = "evidence_submitted"
    TECH_APPROVED = "tech_approved"     # 技术复核通过
    FINANCE_APPROVED = "finance_approved"  # 双复核通过，可拨款
    RELEASED = "released"
    FAILED = "failed"                   # 技术里程碑失败
    BLOCKED = "blocked"                 # 条件未满足被冻结
    CANCELLED = "cancelled"             # 项目终止后取消


@dataclass(frozen=True, slots=True)
class Milestone:
    milestone_id: str
    case_code: str
    seq: int
    name: str
    criteria: str                       # 可验证的技术验收标准
    amount: Money
    status: str = MilestoneStatus.PLANNED
    required_conditions: list[str] = field(default_factory=list)
    evidence_ref: str | None = None
    evidence_submitted_at: str | None = None
    tech_review_ref: str | None = None
    tech_reviewer: str | None = None
    tech_reviewed_at: str | None = None
    finance_review_ref: str | None = None
    finance_reviewer: str | None = None
    finance_reviewed_at: str | None = None


@dataclass(frozen=True, slots=True)
class Contract:
    contract_id: str
    case_code: str
    decision_version: int
    total_amount: Money
    signed_at: str
    milestones: list[Milestone]
    currency: str = "CNY"


# ---------------------------------------------------------------- 拨款与台账

class DisbursementStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    RECALLED = "recalled"


@dataclass(frozen=True, slots=True)
class Disbursement:
    """一笔拨款；同一幂等键只能成功支付一次。"""

    disbursement_id: str
    idempotency_key: str
    case_code: str
    milestone_id: str
    amount: Money
    status: str
    requested_at: str
    paid_at: str | None
    reference: str | None
    source_account: str


@dataclass(frozen=True, slots=True)
class Recall:
    """拨款撤回（如里程碑事后判定失败），钱退回冻结池。"""

    recall_id: str
    disbursement_id: str
    case_code: str
    amount: Money
    reason: str
    recalled_at: str
    recalled_by: str


@dataclass(frozen=True, slots=True)
class Termination:
    case_code: str
    terminated_at: str
    terminated_by: str
    reason: str
    milestone_failure: bool
    unreleased_cancelled: Money        # 被取消的未释放额度（守恒核对用）


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """复式分录的一条腿。金额守恒：同一事务内借贷相等。"""

    entry_id: str
    tx_id: str
    tx_type: str
    posted_at: str
    account: str
    debit: int                          # 整数分
    credit: int
    currency: str
    case_code: str | None
    ref_id: str | None
    memo: str


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """哈希链审计记录：任何改写都会断裂后续链。"""

    seq: int
    occurred_at: str
    actor: str
    action: str
    aggregate: str                      # case / contract / fund ...
    aggregate_id: str
    payload_fingerprint: str
    prev_hash: str
    tx_id: str | None

    def hash(self) -> str:
        return canonical_fingerprint({
            "seq": self.seq,
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "action": self.action,
            "aggregate": self.aggregate,
            "aggregate_id": self.aggregate_id,
            "payload_fingerprint": self.payload_fingerprint,
            "prev_hash": self.prev_hash,
            "tx_id": self.tx_id,
        })


@dataclass(frozen=True, slots=True)
class FundSnapshot:
    """任意日期的资金视图。"""

    as_of: str
    total_fund: Money
    committed: Money
    paid: Money
    frozen: Money
    available: Money
    by_source: dict[str, dict[str, int]]
    currency: str = "CNY"
