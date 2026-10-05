"""应用服务：命令处理（写入）与只读查询。

每个命令：在单个 BEGIN IMMEDIATE 事务内加载聚合→校验→追加事件，
通过命令级幂等键 + 支付流水号唯一约束双重保证"重复请求不多付一笔"。
"""

from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from . import domain as d
from .domain import (
    AGG_CASE, AGG_FUND, AGG_MILESTONE, AGG_POOL, POOL_ID,
    SCORE_DIMENSIONS, TRACKS,
    ST_APPROVED, ST_CONDITIONAL, ST_CONTRACTED, ST_CREATED, ST_IN_REVIEW,
    ST_REJECTED, ST_TERMINATED, ST_COMPLETED,
    R_OPEN, R_CLOSED,
    M_PENDING, M_EVIDENCE, M_TECH_PASSED, M_REVIEWED, M_FAILED, M_PAID,
    M_CLAWED_BACK, M_CANCELLED,
    DEFAULT_APPROVE_LINE, DEFAULT_CONDITIONAL_LINE,
)
from .errors import (
    ConflictOfInterestError, ConflictStateError, NotFoundError, ValidationError,
)
from .events import EventStore, canonical_json
from .ledger import (
    ACC_COMMITTED, ACC_FROZEN, ACC_PAID,
    project_entries, snapshot as ledger_snapshot,
)
from .money import to_cents

# 材料版本必须包含的四个部分（技术路线、团队、资金用途、关联方）
APPLICATION_SECTIONS = ("technical_route", "team", "use_of_funds", "related_parties")


def _fingerprint(body: dict) -> str:
    return canonical_json(body)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class FundService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ============ 内部工具 ============

    def _exec(self, idem_key: str | None, body: dict, handler) -> dict:
        events, replayed = self.store.run_command(
            idem_key, _fingerprint(body) if idem_key is not None else "", handler
        )
        return {
            "replayed": replayed,
            "events": [
                {"seq": e.seq, "type": e.event_type, "aggregate_id": e.aggregate_id,
                 "version": e.version, "at": e.recorded_at}
                for e in events
            ],
        }

    @staticmethod
    def _state(ctx, aggregate_id: str, agg: str, label: str):
        state = d.fold(ctx.load(aggregate_id), agg)
        if state is None:
            raise NotFoundError(f"{label} 不存在: {aggregate_id}")
        return state

    @staticmethod
    def _fund_balance(ctx, fund_id: str):
        return ledger_snapshot(project_entries(ctx.load_all()), fund_id)

    # ============ 基金 ============

    def establish_fund(self, fund_id: str, code: str, name: str, sources: list[dict],
                       actor: str, idem_key: str | None = None) -> dict:
        d.require(code, "基金代码"); d.require(name, "基金名称"); d.require(actor, "操作人")
        if not sources:
            raise ValidationError("基金至少要有一个资金来源")
        norm_sources, seen = [], set()
        for s in sources:
            sid = d.require(str(s.get("source_id", "")), "资金来源标识")
            if sid in seen:
                raise ValidationError(f"资金来源重复: {sid}")
            seen.add(sid)
            cents = to_cents(s.get("amount", 0))
            if cents <= 0:
                raise ValidationError(f"来源 {sid} 金额必须为正")
            norm_sources.append({
                "source_id": sid,
                "name": d.require(str(s.get("name", "")), f"来源 {sid} 名称"),
                "amount_cents": cents,
                "kind": str(s.get("kind", "")),
            })
        body = {"cmd": "establish_fund", "fund_id": fund_id, "code": code,
                "name": name, "sources": norm_sources, "actor": actor}

        def handler(ctx):
            if ctx.load(fund_id):
                raise ConflictStateError(f"基金 {fund_id} 已设立")
            ctx.append(fund_id, AGG_FUND, d.FUND_ESTABLISHED, {
                "fund_id": fund_id, "code": code, "name": name,
                "total_cents": sum(s["amount_cents"] for s in norm_sources),
                "sources": norm_sources, "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def release_allocation(self, fund_id: str, amount, reason: str, actor: str,
                           case_id: str | None = None, milestone_id: str | None = None,
                           from_account: str = ACC_COMMITTED,
                           idem_key: str | None = None) -> dict:
        cents = to_cents(amount)
        if cents <= 0:
            raise ValidationError("释放金额必须为正")
        if from_account not in (ACC_COMMITTED, ACC_FROZEN):
            raise ValidationError("只能从已承诺或冻结科目释放额度")
        d.require(reason, "释放原因")
        body = {"cmd": "release_allocation", "fund_id": fund_id, "amount_cents": cents,
                "reason": reason, "case_id": case_id, "milestone_id": milestone_id,
                "from_account": from_account, "actor": actor}

        def handler(ctx):
            self._state(ctx, fund_id, AGG_FUND, "基金")
            snap = self._fund_balance(ctx, fund_id)
            if case_id:
                rows = [c for c in snap.by_case if c["case_id"] == case_id]
                if not rows:
                    raise ConflictStateError(f"项目 {case_id} 在该基金下无占用")
                available = rows[0][f"{from_account}_cents"]
            elif from_account == ACC_COMMITTED:
                available = snap.committed_cents
            else:
                available = snap.frozen_cents
            if available < cents:
                raise ConflictStateError(
                    f"{from_account} 余额 {available} 分不足以释放 {cents} 分"
                )
            ctx.append(fund_id, AGG_FUND, d.ALLOCATION_RELEASED, {
                "fund_id": fund_id, "case_id": case_id, "milestone_id": milestone_id,
                "amount_cents": cents, "from_account": from_account,
                "reason": reason, "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    # ============ 评委与回避 ============

    def register_reviewer(self, reviewer_id: str, name: str, org: str = "",
                          expertise: list[str] | None = None,
                          idem_key: str | None = None) -> dict:
        d.require(reviewer_id, "评委标识"); d.require(name, "评委姓名")
        expertise = expertise or []
        body = {"cmd": "register_reviewer", "reviewer_id": reviewer_id, "name": name,
                "org": org, "expertise": expertise}

        def handler(ctx):
            pool = d.fold(ctx.load(POOL_ID), AGG_POOL)
            if pool is not None and reviewer_id in pool.reviewers:
                raise ConflictStateError(f"评委 {reviewer_id} 已注册")
            ctx.append(POOL_ID, AGG_POOL, d.REVIEWER_REGISTERED, {
                "reviewer_id": reviewer_id, "name": name, "org": org.strip(),
                "expertise": expertise, "at": ctx.now(),
            }, "system")

        return self._exec(idem_key, body, handler)

    def deactivate_reviewer(self, reviewer_id: str, reason: str, actor: str = "system",
                            idem_key: str | None = None) -> dict:
        body = {"cmd": "deactivate_reviewer", "reviewer_id": reviewer_id, "reason": reason}

        def handler(ctx):
            pool = self._state(ctx, POOL_ID, AGG_POOL, "评委名册")
            if reviewer_id not in pool.reviewers:
                raise NotFoundError(f"评委不存在: {reviewer_id}")
            if not pool.reviewers[reviewer_id]["active"]:
                raise ConflictStateError(f"评委 {reviewer_id} 已停用")
            ctx.append(POOL_ID, AGG_POOL, d.REVIEWER_DEACTIVATED, {
                "reviewer_id": reviewer_id, "reason": reason, "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def declare_relationship(self, reviewer_id: str, related_party: str,
                             relation_type: str, detail: str = "",
                             idem_key: str | None = None) -> dict:
        d.require(related_party, "关联方")
        d.require(relation_type, "关系类型")
        body = {"cmd": "declare_relationship", "reviewer_id": reviewer_id,
                "related_party": related_party, "relation_type": relation_type,
                "detail": detail}

        def handler(ctx):
            pool = self._state(ctx, POOL_ID, AGG_POOL, "评委名册")
            if reviewer_id not in pool.reviewers:
                raise NotFoundError(f"评委不存在: {reviewer_id}")
            ctx.append(POOL_ID, AGG_POOL, d.RELATIONSHIP_DECLARED, {
                "reviewer_id": reviewer_id,
                "related_party": related_party.strip(),
                "relation_type": relation_type.strip(),
                "detail": detail.strip(), "at": ctx.now(),
            }, reviewer_id)

        return self._exec(idem_key, body, handler)

    # ============ 项目与材料版本 ============

    def create_case(self, case_id: str, code: str, track: str, applicant: str,
                    applicant_org: str, requested_amount, actor: str,
                    idem_key: str | None = None) -> dict:
        d.require(code, "项目编号"); d.require(applicant, "申请人")
        if track not in TRACKS:
            raise ValidationError(f"赛道必须是 {TRACKS} 之一")
        cents = to_cents(requested_amount)
        if cents <= 0:
            raise ValidationError("申请额度必须为正")
        body = {"cmd": "create_case", "case_id": case_id, "code": code, "track": track,
                "applicant": applicant, "applicant_org": applicant_org,
                "requested_cents": cents, "actor": actor}

        def handler(ctx):
            if ctx.load(case_id):
                raise ConflictStateError(f"项目 {case_id} 已存在")
            for e in ctx.load_all():
                if e.event_type == d.CASE_CREATED and e.payload["code"] == code:
                    raise ConflictStateError(f"项目编号 {code} 已被占用")
            ctx.append(case_id, AGG_CASE, d.CASE_CREATED, {
                "case_id": case_id, "code": code, "track": track,
                "applicant": applicant.strip(), "applicant_org": applicant_org.strip(),
                "requested_cents": cents, "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def submit_application(self, case_id: str, technical_route, team, use_of_funds,
                           related_parties: list[dict], actor: str,
                           version_label: str = "", documents: list[dict] | None = None,
                           change_summary: str = "", idem_key: str | None = None) -> dict:
        snapshot_payload = {
            "version_label": version_label.strip(),
            "technical_route": self._require_section(technical_route, "技术路线"),
            "team": self._require_section(team, "团队"),
            "use_of_funds": self._require_section(use_of_funds, "资金用途"),
            "related_parties": self._normalize_parties(related_parties),
            "documents": documents or [],
            "change_summary": change_summary.strip(),
        }
        body = {"cmd": "submit_application", "case_id": case_id,
                "snapshot": snapshot_payload, "actor": actor}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.status not in (ST_CREATED, ST_IN_REVIEW):
                raise ConflictStateError(f"项目当前状态 {case.status} 不允许提交/变更材料")
            rnd = case.rounds.get(case.current_round_no)
            if rnd is not None and rnd.status == R_OPEN:
                raise ConflictStateError("评审轮次已开启，材料即冻结；变更请在新一轮评审提交")
            prev_hash = case.current_version["hash"] if case.current_version else None
            version_no = len(case.versions) + 1
            snap = {"version_no": version_no, **snapshot_payload}
            snap["hash"] = canonical_json(
                {k: v for k, v in snap.items() if k != "hash"}
            )
            snap["prev_version_hash"] = prev_hash
            snap["submitted_at"] = ctx.now()
            ctx.append(case_id, AGG_CASE, d.APPLICATION_SUBMITTED,
                       {"snapshot": snap}, actor)

        return self._exec(idem_key, body, handler)

    @staticmethod
    def _require_section(value, label: str):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValidationError(f"{label}不能为空")
            return value
        if isinstance(value, (dict, list)) and value:
            return value
        raise ValidationError(f"{label}必须是非空字符串、对象或数组")

    @staticmethod
    def _normalize_parties(parties) -> list[dict]:
        # 显式空列表也是一种声明：本版本无关联方
        if not isinstance(parties, list):
            raise ValidationError("关联方清单必须是数组（无关联方时显式提交空数组）")
        out = []
        for item in parties:
            if not isinstance(item, dict):
                raise ValidationError("关联方条目必须是对象")
            party = str(item.get("party", "")).strip()
            if not party:
                raise ValidationError("关联方名称不能为空")
            out.append({"party": party,
                        "relationship": str(item.get("relationship", "")).strip()})
        return out

    # ============ 评审轮次、回避分配 ============

    def open_review_round(self, case_id: str, actor: str = "system",
                          idem_key: str | None = None) -> dict:
        body = {"cmd": "open_review_round", "case_id": case_id}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if not case.versions:
                raise ConflictStateError("尚未提交申请材料，无法开启评审")
            cur = case.rounds.get(case.current_round_no)
            if cur is not None and cur.status == R_OPEN:
                raise ConflictStateError(f"第 {cur.round_no} 轮评审仍在进行")
            if case.status not in (ST_CREATED, ST_IN_REVIEW):
                raise ConflictStateError(f"项目状态 {case.status} 不允许开启评审")
            round_no = (max(case.rounds, default=0)) + 1
            ctx.append(case_id, AGG_CASE, d.REVIEW_ROUND_OPENED, {
                "round_no": round_no, "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    @staticmethod
    def _conflict_parties(case) -> set[str]:
        parties = {case.applicant.casefold(), case.applicant_org.casefold()} - {""}
        ver = case.current_version
        if ver:
            for rp in ver.get("related_parties", []):
                parties.add(rp["party"].casefold())
        return parties

    def assign_reviewers(self, case_id: str, actor: str = "system",
                         reviewer_ids: list[str] | None = None,
                         min_reviewers: int = 3,
                         idem_key: str | None = None) -> dict:
        if min_reviewers < 1:
            raise ValidationError("每轮至少需要 1 名评委")
        body = {"cmd": "assign_reviewers", "case_id": case_id,
                "reviewer_ids": reviewer_ids, "min_reviewers": min_reviewers}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            pool = self._state(ctx, POOL_ID, AGG_POOL, "评委名册")
            rnd = case.round()
            if rnd.status != R_OPEN:
                raise ConflictStateError("当前评审轮次未开启或已关闭")
            if rnd.assignments:
                raise ConflictStateError("本轮已完成评委分配")
            parties = self._conflict_parties(case)

            def conflict_reason(rid: str, reviewer: dict) -> str | None:
                org = reviewer.get("org", "").strip()
                if org and org.casefold() in parties:
                    return f"所属机构 {org} 与申请方/关联方重合"
                if reviewer["name"].casefold() in parties:
                    return "评委本人为申请人或关联方"
                for rel in reviewer.get("relations", []):
                    if rel["related_party"].casefold() in parties:
                        return f"已申报关系：{rel['relation_type']}（{rel['related_party']}）"
                return None

            assignments, excluded = [], []
            for rid in sorted(pool.reviewers):
                reviewer = pool.reviewers[rid]
                if not reviewer["active"]:
                    excluded.append({"reviewer_id": rid, "name": reviewer["name"],
                                     "reason": "评委已停用"})
                    continue
                reason = conflict_reason(rid, reviewer)
                if reason:
                    excluded.append({"reviewer_id": rid, "name": reviewer["name"],
                                     "reason": reason})
                else:
                    assignments.append({"reviewer_id": rid, "name": reviewer["name"],
                                        "org": reviewer["org"],
                                        "expertise": reviewer["expertise"]})
            if reviewer_ids:
                wanted = list(dict.fromkeys(reviewer_ids))
                by_id = {a["reviewer_id"]: a for a in assignments}
                ex_by_id = {e["reviewer_id"]: e for e in excluded}
                chosen, missing = [], set()
                for rid in wanted:
                    if rid in by_id:
                        chosen.append(by_id[rid])
                    elif rid in ex_by_id:
                        raise ConflictOfInterestError(
                            f"评委 {rid} 应回避：{ex_by_id[rid]['reason']}"
                        )
                    elif rid in pool.reviewers:
                        raise ConflictStateError(f"评委 {rid} 已停用，不能参与")
                    else:
                        missing.add(rid)
                if missing:
                    raise NotFoundError(f"评委不存在: {sorted(missing)}")
                assignments = chosen
            else:
                assignments = assignments[:min_reviewers]

            if len(assignments) < min_reviewers:
                raise ConflictStateError(
                    f"无回避关系的合格评委不足：需要 {min_reviewers} 名，"
                    f"仅有 {len(assignments)} 名"
                )
            ctx.append(case_id, AGG_CASE, d.REVIEWERS_ASSIGNED, {
                "round_no": rnd.round_no,
                "assignments": assignments,
                "excluded": excluded,
                "note": "按回避关系自动筛选；excluded 记录全部排除原因",
                "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    # ============ 评分、质询 ============

    def submit_score(self, case_id: str, reviewer_id: str, scores: dict[str, float],
                     comment: str = "", idem_key: str | None = None) -> dict:
        clean = {}
        for dim in SCORE_DIMENSIONS:
            if dim not in scores:
                raise ValidationError(f"缺少评分维度: {dim}")
            value = scores[dim]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValidationError(f"维度 {dim} 评分必须是数字")
            if not 0 <= float(value) <= 100:
                raise ValidationError(f"维度 {dim} 评分必须在 0-100 之间")
            clean[dim] = round(float(value), 2)
        body = {"cmd": "submit_score", "case_id": case_id,
                "reviewer_id": reviewer_id, "scores": clean, "comment": comment}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            rnd = case.round()
            if rnd.status != R_OPEN:
                raise ConflictStateError("评审轮次未开启或已关闭")
            if reviewer_id not in rnd.reviewer_ids:
                raise ConflictOfInterestError("该评委未被分配到本轮，无权评分（可能存在回避）")
            if reviewer_id in rnd.scores or reviewer_id in rnd.abstentions:
                raise ConflictStateError("评委已提交表决，评分不可改写")
            total = round(sum(clean.values()) / len(SCORE_DIMENSIONS), 2)
            ctx.append(case_id, AGG_CASE, d.SCORE_SUBMITTED, {
                "round_no": rnd.round_no, "reviewer_id": reviewer_id,
                "scores": clean, "weighted_total": total,
                "weights": {k: round(1 / len(SCORE_DIMENSIONS), 4) for k in SCORE_DIMENSIONS},
                "comment": comment.strip(), "at": ctx.now(),
            }, reviewer_id)

        return self._exec(idem_key, body, handler)

    def abstain_score(self, case_id: str, reviewer_id: str, reason: str,
                      idem_key: str | None = None) -> dict:
        d.require(reason, "回避/弃权原因")
        body = {"cmd": "abstain_score", "case_id": case_id,
                "reviewer_id": reviewer_id, "reason": reason}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            rnd = case.round()
            if rnd.status != R_OPEN:
                raise ConflictStateError("评审轮次未开启或已关闭")
            if reviewer_id not in rnd.reviewer_ids:
                raise ConflictOfInterestError("该评委未被分配到本轮")
            if reviewer_id in rnd.scores or reviewer_id in rnd.abstentions:
                raise ConflictStateError("评委已提交表决，不能再变更为回避")
            ctx.append(case_id, AGG_CASE, d.SCORE_SUBMITTED, {
                "round_no": rnd.round_no, "reviewer_id": reviewer_id,
                "abstain": True, "reason": reason, "at": ctx.now(),
            }, reviewer_id)

        return self._exec(idem_key, body, handler)

    def ask_question(self, case_id: str, reviewer_id: str, content: str,
                     idem_key: str | None = None) -> dict:
        d.require(content, "质询内容")
        question_id = _new_id("q")
        body = {"cmd": "ask_question", "case_id": case_id,
                "reviewer_id": reviewer_id, "content": content, "question_id": question_id}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            rnd = case.round()
            if rnd.status != R_OPEN:
                raise ConflictStateError("评审轮次未开启或已关闭")
            if reviewer_id not in rnd.reviewer_ids:
                raise ConflictOfInterestError("该评委未被分配到本轮，无权质询")
            ctx.append(case_id, AGG_CASE, d.QUESTION_RAISED, {
                "round_no": rnd.round_no, "question_id": question_id,
                "reviewer_id": reviewer_id, "content": content.strip(), "at": ctx.now(),
            }, reviewer_id)

        return self._exec(idem_key, body, handler)

    def answer_question(self, case_id: str, question_id: str, content: str,
                        actor: str, idem_key: str | None = None) -> dict:
        d.require(content, "答复内容")
        body = {"cmd": "answer_question", "case_id": case_id,
                "question_id": question_id, "content": content, "actor": actor}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            rnd = case.round()
            if rnd.status != R_OPEN:
                raise ConflictStateError("评审轮次未开启或已关闭")
            if not any(q["question_id"] == question_id for q in rnd.questions):
                raise NotFoundError(f"质询不存在: {question_id}")
            target = next(q for q in rnd.questions if q["question_id"] == question_id)
            if target.get("answer") is not None:
                raise ConflictStateError("该质询已答复，答复不可改写")
            ctx.append(case_id, AGG_CASE, d.QUESTION_ANSWERED, {
                "round_no": rnd.round_no, "question_id": question_id,
                "content": content.strip(), "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def close_round(self, case_id: str, actor: str = "system",
                    idem_key: str | None = None) -> dict:
        body = {"cmd": "close_round", "case_id": case_id}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            rnd = case.round()
            if rnd.status != R_OPEN:
                raise ConflictStateError("当前没有进行中的评审轮次")
            if not rnd.assignments:
                raise ConflictStateError("尚未分配评委")
            pending = rnd.reviewer_ids - rnd.scores.keys() - rnd.abstentions.keys()
            if pending:
                raise ConflictStateError(f"以下评委尚未表决: {sorted(pending)}")
            open_qs = [q["question_id"] for q in rnd.open_questions]
            if open_qs:
                raise ConflictStateError(f"尚有 {len(open_qs)} 条质询未答复")
            if not rnd.scores:
                raise ConflictStateError("没有任何有效评分（全部回避），不能形成评审结论")
            by_dim = {dim: [s["scores"][dim] for s in rnd.scores.values()]
                      for dim in SCORE_DIMENSIONS}
            totals = [s["weighted_total"] for s in rnd.scores.values()]
            summary = {
                "round_no": rnd.round_no,
                "n_assigned": len(rnd.assignments),
                "n_scored": len(rnd.scores),
                "n_abstained": len(rnd.abstentions),
                "abstentions": [
                    {"reviewer_id": rid, "reason": why}
                    for rid, why in sorted(rnd.abstentions.items())
                ],
                "dimension_avg": {dim: round(sum(v) / len(v), 2)
                                  for dim, v in by_dim.items()},
                "weighted_total_avg": round(sum(totals) / len(totals), 2),
                "score_band": self._score_band(round(sum(totals) / len(totals), 2)),
            }
            ctx.append(case_id, AGG_CASE, d.REVIEW_CLOSED,
                       {**summary, "at": ctx.now()}, actor)

        return self._exec(idem_key, body, handler)

    @staticmethod
    def _score_band(total: float, approve_line: float = DEFAULT_APPROVE_LINE,
                    conditional_line: float = DEFAULT_CONDITIONAL_LINE) -> str:
        if total >= approve_line:
            return "approved"
        if total >= conditional_line:
            return "conditional"
        return "rejected"

    # ============ 决策、申诉 ============

    def make_decision(self, case_id: str, result: str, rationale: str, actor: str,
                      conditions: list[str] | None = None,
                      approve_line: float = DEFAULT_APPROVE_LINE,
                      conditional_line: float = DEFAULT_CONDITIONAL_LINE,
                      idem_key: str | None = None) -> dict:
        if result not in ("approved", "conditional", "rejected"):
            raise ValidationError("决策结果必须是 approved/conditional/rejected")
        d.require(rationale, "决策理由")
        conditions = conditions or []
        if result == "conditional" and not conditions:
            raise ValidationError("条件性通过必须列出前置条件")
        body = {"cmd": "make_decision", "case_id": case_id, "result": result,
                "rationale": rationale, "conditions": conditions,
                "approve_line": approve_line, "conditional_line": conditional_line}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            rnd = case.rounds.get(case.current_round_no)
            if rnd is None or rnd.status != R_CLOSED:
                raise ConflictStateError("当前轮次尚未关闭，不能决策")
            band_total = None
            for e in reversed(ctx.load(case_id)):
                if e.event_type == d.REVIEW_CLOSED and e.payload["round_no"] == rnd.round_no:
                    band_total = e.payload["weighted_total_avg"]
                    summary = {k: e.payload[k] for k in
                               ("n_assigned", "n_scored", "n_abstained",
                                "dimension_avg", "weighted_total_avg", "score_band")}
                    break
            band = self._score_band(band_total, approve_line, conditional_line)
            if result != band:
                raise ConflictStateError(
                    f"决策 {result} 与评分区间 {band}（均分 {band_total}）不一致，"
                    "请先组织新一轮评审或调整规则，不得背离分数决策"
                )
            decision_id = _new_id("dec")
            ctx.append(case_id, AGG_CASE, d.DECISION_MADE, {
                "decision_id": decision_id,
                "round_no": rnd.round_no,
                "result": result,
                "rationale": rationale.strip(),
                "conditions": list(conditions),
                "thresholds": {"approve_line": approve_line,
                               "conditional_line": conditional_line},
                "score_summary": summary,
                "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def satisfy_conditions(self, case_id: str, note: str, actor: str,
                           idem_key: str | None = None) -> dict:
        body = {"cmd": "satisfy_conditions", "case_id": case_id, "note": note}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.status != ST_CONDITIONAL:
                raise ConflictStateError("只有条件性通过的项目才能确认条件满足")
            ctx.append(case_id, AGG_CASE, d.CONDITIONS_SATISFIED, {
                "note": note.strip(), "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def appeal_decision(self, case_id: str, reason: str, actor: str,
                        evidence: list[dict] | None = None,
                        idem_key: str | None = None) -> dict:
        d.require(reason, "申诉理由")
        appeal_id = _new_id("appeal")
        body = {"cmd": "appeal_decision", "case_id": case_id, "reason": reason,
                "evidence": evidence or [], "appeal_id": appeal_id}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.status not in (ST_APPROVED, ST_CONDITIONAL, ST_REJECTED):
                raise ConflictStateError("当前没有可申诉的决策")
            if case.appeal is not None and case.appeal.get("decision_id") == \
                    case.decision["decision_id"] and "ruling" not in case.appeal:
                raise ConflictStateError("该决策的申诉正在处理中")
            ctx.append(case_id, AGG_CASE, d.DECISION_APPEALED, {
                "appeal_id": appeal_id,
                "decision_id": case.decision["decision_id"],
                "reason": reason.strip(), "evidence": evidence or [],
                "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def rule_appeal(self, case_id: str, ruling: str, note: str, actor: str,
                    idem_key: str | None = None) -> dict:
        if ruling not in ("uphold", "reopen"):
            raise ValidationError("申诉裁决必须是 uphold 或 reopen")
        d.require(note, "裁决说明")
        body = {"cmd": "rule_appeal", "case_id": case_id, "ruling": ruling, "note": note}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.appeal is None or "ruling" in case.appeal:
                raise ConflictStateError("没有待裁决的申诉")
            ctx.append(case_id, AGG_CASE, d.APPEAL_RULED, {
                "appeal_id": case.appeal["appeal_id"],
                "ruling": ruling, "note": note.strip(), "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    # ============ 合同与里程碑 ============

    def sign_contract(self, case_id: str, fund_id: str, milestones: list[dict],
                      actor: str, idem_key: str | None = None) -> dict:
        if not milestones:
            raise ValidationError("合同至少包含一个里程碑")
        norm_ms, ids = [], set()
        for i, m in enumerate(milestones, start=1):
            mid = d.require(str(m.get("milestone_id", "")), f"里程碑 {i} 标识")
            if mid in ids:
                raise ValidationError(f"里程碑标识重复: {mid}")
            ids.add(mid)
            cents = to_cents(m.get("amount", 0))
            if cents <= 0:
                raise ValidationError(f"里程碑 {mid} 金额必须为正")
            norm_ms.append({
                "milestone_id": mid,
                "seq": int(m.get("seq") or i),
                "name": d.require(str(m.get("name", "")), f"里程碑 {mid} 名称"),
                "description": str(m.get("description", "")).strip(),
                "due_date": str(m.get("due_date", "")).strip(),
                "amount_cents": cents,
                "criteria": str(m.get("criteria", "")).strip(),
            })
        total = sum(m["amount_cents"] for m in norm_ms)
        body = {"cmd": "sign_contract", "case_id": case_id, "fund_id": fund_id,
                "milestones": norm_ms, "total_cents": total, "actor": actor}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.status != ST_APPROVED:
                raise ConflictStateError(f"项目状态 {case.status}，未正式通过不能签合同")
            if case.appeal is not None and "ruling" not in case.appeal:
                raise ConflictStateError("决策申诉尚未裁决，不能签署合同")
            if case.contract is not None:
                raise ConflictStateError("项目已签署合同")
            fund = self._state(ctx, fund_id, AGG_FUND, "基金")
            if case.requested_cents and total > case.requested_cents:
                raise ConflictStateError("合同总额超过申请额度")
            snap = self._fund_balance(ctx, fund_id)
            if snap.available_cents < total:
                raise ConflictStateError(
                    f"基金可用资金 {snap.available_cents} 分不足，合同需要 {total} 分"
                )
            for m in norm_ms:
                if ctx.load(m["milestone_id"]):
                    raise ConflictStateError(f"里程碑 {m['milestone_id']} 已存在")
            at = ctx.now()
            for m in norm_ms:
                ctx.append(m["milestone_id"], AGG_MILESTONE, d.MILESTONE_DEFINED, {
                    "milestone_id": m["milestone_id"], "case_id": case_id,
                    "seq": m["seq"], "name": m["name"],
                    "description": m["description"], "due_date": m["due_date"],
                    "amount_cents": m["amount_cents"], "criteria": m["criteria"],
                    "fund_id": fund_id, "at": at,
                }, actor)
            ctx.append(case_id, AGG_CASE, d.CONTRACT_SIGNED, {
                "case_id": case_id, "fund_id": fund_id,
                "milestone_ids": [m["milestone_id"] for m in norm_ms],
                "milestone_amounts": [[m["milestone_id"], m["amount_cents"]]
                                      for m in norm_ms],
                "total_amount_cents": total, "signed_at": at,
            }, actor)

        return self._exec(idem_key, body, handler)

    def flag_failure(self, case_id: str, reason: str, actor: str,
                     evidence: list[dict] | None = None,
                     idem_key: str | None = None) -> dict:
        d.require(reason, "失败原因")
        body = {"cmd": "flag_failure", "case_id": case_id, "reason": reason,
                "evidence": evidence or []}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.status != ST_CONTRACTED:
                raise ConflictStateError("只有已签约项目可以标记技术里程碑失败")
            if case.failure_flag is not None:
                raise ConflictStateError("已存在未解除的失败标记")
            ctx.append(case_id, AGG_CASE, d.FAILURE_FLAG_RAISED, {
                "reason": reason.strip(), "evidence": evidence or [], "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def waive_failure(self, case_id: str, reason: str, actor: str,
                      idem_key: str | None = None) -> dict:
        d.require(reason, "豁免原因")
        body = {"cmd": "waive_failure", "case_id": case_id, "reason": reason}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.failure_flag is None:
                raise ConflictStateError("没有待解除的失败标记")
            ctx.append(case_id, AGG_CASE, d.FAILURE_WAIVED, {
                "reason": reason.strip(), "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def terminate_case(self, case_id: str, reason: str, actor: str,
                       idem_key: str | None = None) -> dict:
        d.require(reason, "终止原因")
        body = {"cmd": "terminate_case", "case_id": case_id, "reason": reason}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.status not in (ST_CONTRACTED, ST_APPROVED, ST_CONDITIONAL):
                raise ConflictStateError(f"项目状态 {case.status} 不能终止")
            at = ctx.now()
            for mid in case.milestone_ids:
                m = self._state(ctx, mid, AGG_MILESTONE, "里程碑")
                if m.status == M_REVIEWED:
                    # 已冻结未付：解冻(frozen->committed) -> 释放(committed->可用) -> 取消
                    ctx.append(mid, AGG_MILESTONE, d.MILESTONE_UNFROZEN, {
                        "fund_id": case.fund_id, "case_id": case_id,
                        "milestone_id": mid, "amount_cents": m.financial["approved_cents"],
                        "reason": f"项目终止：{reason}", "at": at,
                    }, actor)
                    ctx.append(case.fund_id, AGG_FUND, d.ALLOCATION_RELEASED, {
                        "fund_id": case.fund_id, "case_id": case_id,
                        "milestone_id": mid, "amount_cents": m.financial["approved_cents"],
                        "from_account": ACC_COMMITTED,
                        "reason": f"项目终止：{reason}", "at": at,
                    }, actor)
                    ctx.append(mid, AGG_MILESTONE, d.MILESTONE_CANCELLED,
                               {"reason": reason, "at": at}, actor)
                elif m.status in (M_PENDING, M_EVIDENCE, M_TECH_PASSED):
                    # 尚未复核通过：取消并把承诺额度原路退回可用
                    ctx.append(case.fund_id, AGG_FUND, d.ALLOCATION_RELEASED, {
                        "fund_id": case.fund_id, "case_id": case_id,
                        "milestone_id": mid, "amount_cents": m.amount_cents,
                        "from_account": ACC_COMMITTED,
                        "reason": f"项目终止：{reason}", "at": at,
                    }, actor)
                    ctx.append(mid, AGG_MILESTONE, d.MILESTONE_CANCELLED,
                               {"reason": reason, "at": at}, actor)
                elif m.status == M_FAILED:
                    # 复核失败时承诺额度已释放，仅取消里程碑
                    ctx.append(mid, AGG_MILESTONE, d.MILESTONE_CANCELLED,
                               {"reason": reason, "at": at}, actor)
                # 已付/已撤回/已取消不动；已付款项的追讨走 clawback
            ctx.append(case_id, AGG_CASE, d.CASE_TERMINATED, {
                "reason": reason.strip(), "at": at,
            }, actor)

        return self._exec(idem_key, body, handler)

    # ============ 里程碑证据、复核、拨款 ============

    def submit_evidence(self, milestone_id: str, files: list[dict], summary: str,
                        actor: str, idem_key: str | None = None) -> dict:
        if not files:
            raise ValidationError("至少提交一份证据材料")
        clean = []
        for f in files:
            name = d.require(str(f.get("name", "")), "证据文件名")
            digest = d.require(str(f.get("hash", "")), f"证据 {name} 的哈希")
            clean.append({"name": name, "hash": digest,
                          "uri": str(f.get("uri", "")).strip()})
        d.require(summary, "证据说明")
        evidence_id = _new_id("ev")
        body = {"cmd": "submit_evidence", "milestone_id": milestone_id,
                "files": clean, "summary": summary, "evidence_id": evidence_id}

        def handler(ctx):
            m = self._state(ctx, milestone_id, AGG_MILESTONE, "里程碑")
            case = self._state(ctx, m.case_id, AGG_CASE, "项目")
            if case.status == ST_TERMINATED:
                raise ConflictStateError("项目已终止，不能再提交证据")
            if case.failure_flag is not None:
                raise ConflictStateError("项目存在未解除的技术失败标记，拨款流程已中止")
            if m.status not in (M_PENDING, M_EVIDENCE):
                raise ConflictStateError(f"里程碑状态 {m.status}，不能再提交证据")
            ctx.append(milestone_id, AGG_MILESTONE, d.EVIDENCE_SUBMITTED, {
                "evidence_id": evidence_id, "files": clean,
                "summary": summary.strip(), "submitter": actor, "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def review_technical(self, milestone_id: str, passed: bool, opinion: str,
                         reviewer: str, idem_key: str | None = None) -> dict:
        d.require(opinion, "技术复核意见")
        body = {"cmd": "review_technical", "milestone_id": milestone_id,
                "passed": bool(passed), "opinion": opinion, "reviewer": reviewer}

        def handler(ctx):
            m = self._state(ctx, milestone_id, AGG_MILESTONE, "里程碑")
            case = self._state(ctx, m.case_id, AGG_CASE, "项目")
            if case.failure_flag is not None:
                raise ConflictStateError("项目存在未解除的技术失败标记，拨款流程已中止")
            if m.technical is not None:
                raise ConflictStateError("技术复核已完成，结论不可改写")
            if not m.evidence:
                raise ConflictStateError("尚无证据材料，不能进行技术复核")
            if m.status in (M_PAID, M_CLAWED_BACK, M_CANCELLED, M_FAILED):
                raise ConflictStateError(f"里程碑状态 {m.status}，不能复核")
            at = ctx.now()
            if passed:
                ctx.append(milestone_id, AGG_MILESTONE, d.TECHNICAL_REVIEW_PASSED, {
                    "passed": True, "opinion": opinion.strip(),
                    "reviewer": reviewer, "at": at,
                }, reviewer)
            else:
                ctx.append(milestone_id, AGG_MILESTONE, d.TECHNICAL_REVIEW_FAILED, {
                    "passed": False, "opinion": opinion.strip(),
                    "reviewer": reviewer, "at": at,
                }, reviewer)
                # 失败即释放该里程碑的承诺额度，及时停止后续拨款
                ctx.append(case.fund_id, AGG_FUND, d.ALLOCATION_RELEASED, {
                    "fund_id": case.fund_id, "case_id": m.case_id,
                    "milestone_id": milestone_id, "amount_cents": m.amount_cents,
                    "from_account": ACC_COMMITTED,
                    "reason": f"技术复核未通过：{opinion.strip()[:80]}", "at": at,
                }, reviewer)

        return self._exec(idem_key, body, handler)

    def review_financial(self, milestone_id: str, passed: bool, opinion: str,
                         reviewer: str, eligible_amount=None,
                         idem_key: str | None = None) -> dict:
        d.require(opinion, "财务复核意见")
        body = {"cmd": "review_financial", "milestone_id": milestone_id,
                "passed": bool(passed), "opinion": opinion, "reviewer": reviewer,
                "eligible_amount": str(eligible_amount) if eligible_amount is not None else None}

        def handler(ctx):
            m = self._state(ctx, milestone_id, AGG_MILESTONE, "里程碑")
            case = self._state(ctx, m.case_id, AGG_CASE, "项目")
            if case.failure_flag is not None:
                raise ConflictStateError("项目存在未解除的技术失败标记，拨款流程已中止")
            if m.financial is not None:
                raise ConflictStateError("财务复核已完成，结论不可改写")
            if m.status != M_TECH_PASSED:
                raise ConflictStateError("技术复核通过后才能进行财务复核")
            at = ctx.now()
            if not passed:
                ctx.append(milestone_id, AGG_MILESTONE, d.FINANCIAL_REVIEW_FAILED, {
                    "passed": False, "opinion": opinion.strip(),
                    "reviewer": reviewer, "at": at,
                }, reviewer)
                ctx.append(case.fund_id, AGG_FUND, d.ALLOCATION_RELEASED, {
                    "fund_id": case.fund_id, "case_id": m.case_id,
                    "milestone_id": milestone_id, "amount_cents": m.amount_cents,
                    "from_account": ACC_COMMITTED,
                    "reason": f"财务复核未通过：{opinion.strip()[:80]}", "at": at,
                }, reviewer)
                return
            approved = m.amount_cents if eligible_amount is None else to_cents(eligible_amount)
            if not 0 < approved <= m.amount_cents:
                raise ValidationError("核定金额必须为正且不超过里程碑金额")
            ctx.append(milestone_id, AGG_MILESTONE, d.FINANCIAL_REVIEW_PASSED, {
                "passed": True, "opinion": opinion.strip(), "reviewer": reviewer,
                "approved_cents": approved, "at": at,
            }, reviewer)
            if approved < m.amount_cents:
                ctx.append(case.fund_id, AGG_FUND, d.ALLOCATION_RELEASED, {
                    "fund_id": case.fund_id, "case_id": m.case_id,
                    "milestone_id": milestone_id,
                    "amount_cents": m.amount_cents - approved,
                    "from_account": ACC_COMMITTED,
                    "reason": "财务复核核减", "at": at,
                }, reviewer)
            # 双复核通过即冻结核定额度，等待放款
            ctx.append(milestone_id, AGG_MILESTONE, d.MILESTONE_FROZEN, {
                "fund_id": case.fund_id, "case_id": m.case_id,
                "milestone_id": milestone_id, "amount_cents": approved, "at": at,
            }, reviewer)

        return self._exec(idem_key, body, handler)

    def release_payment(self, milestone_id: str, payment_ref: str, actor: str,
                        idem_key: str | None = None) -> dict:
        d.require(payment_ref, "支付流水号")
        body = {"cmd": "release_payment", "milestone_id": milestone_id,
                "payment_ref": payment_ref, "actor": actor}

        def handler(ctx):
            m = self._state(ctx, milestone_id, AGG_MILESTONE, "里程碑")
            existing = ctx.conn.execute(
                "SELECT event_seq FROM payment_refs WHERE payment_ref = ?", (payment_ref,)
            ).fetchone()
            if existing is not None:
                raise ConflictStateError(
                    f"支付流水号 {payment_ref} 已支付（事件 {existing['event_seq']}），"
                    "拒绝重复付款；如需再次拨款请使用新流水号并先走复核流程"
                )
            case = self._state(ctx, m.case_id, AGG_CASE, "项目")
            if case.status == ST_TERMINATED:
                raise ConflictStateError("项目已终止，禁止付款")
            if case.failure_flag is not None:
                raise ConflictStateError("项目存在未解除的技术失败标记，禁止付款")
            if m.status != M_REVIEWED or not m.frozen_at:
                raise ConflictStateError(f"里程碑状态 {m.status}，未完成双重复核与冻结，不能付款")
            amount = m.financial["approved_cents"]
            event = ctx.append(milestone_id, AGG_MILESTONE, d.MILESTONE_PAID, {
                "fund_id": case.fund_id, "case_id": m.case_id,
                "milestone_id": milestone_id, "amount_cents": amount,
                "payment_ref": payment_ref, "at": ctx.now(),
            }, actor)
            # 第二道幂等防线：即使不带 idem_key，流水号重复也绝不可能二次付款
            ctx.reserve_payment_ref(payment_ref, event.seq)

        return self._exec(idem_key, body, handler)

    def clawback_payment(self, milestone_id: str, reason: str, actor: str,
                         amount=None, idem_key: str | None = None) -> dict:
        d.require(reason, "撤回原因")
        body = {"cmd": "clawback_payment", "milestone_id": milestone_id,
                "reason": reason, "amount": str(amount) if amount is not None else None}

        def handler(ctx):
            m = self._state(ctx, milestone_id, AGG_MILESTONE, "里程碑")
            if m.status not in (M_PAID, M_CLAWED_BACK):
                raise ConflictStateError(f"里程碑状态 {m.status}，没有可撤回的拨款")
            paid_total = m.financial["approved_cents"]
            already = m.clawback["cumulative_cents"] if m.clawback else 0
            remaining_paid = paid_total - already
            amount_cents = remaining_paid if amount is None else to_cents(amount)
            if not 0 < amount_cents <= remaining_paid:
                raise ConflictStateError(
                    f"撤回金额 {amount_cents} 超过剩余已付 {remaining_paid}"
                )
            cumulative = already + amount_cents
            ctx.append(milestone_id, AGG_MILESTONE, d.MILESTONE_CLAWED_BACK, {
                "fund_id": m.fund_id,
                "case_id": m.case_id, "milestone_id": milestone_id,
                "payment_ref": m.payment_ref,
                "amount_cents": amount_cents,
                "cumulative_cents": cumulative,
                "remaining_paid_cents": paid_total - cumulative,
                "full": cumulative >= paid_total,
                "reason": reason.strip(), "at": ctx.now(),
            }, actor)

        return self._exec(idem_key, body, handler)

    def complete_case(self, case_id: str, actor: str = "system",
                      idem_key: str | None = None) -> dict:
        body = {"cmd": "complete_case", "case_id": case_id}

        def handler(ctx):
            case = self._state(ctx, case_id, AGG_CASE, "项目")
            if case.status != ST_CONTRACTED:
                raise ConflictStateError("只有履约中的项目可以结项")
            for mid in case.milestone_ids:
                m = self._state(ctx, mid, AGG_MILESTONE, "里程碑")
                if m.status not in (M_PAID, M_CLAWED_BACK, M_CANCELLED, M_FAILED):
                    raise ConflictStateError(f"里程碑 {mid} 状态 {m.status}，未了结")
            ctx.append(case_id, AGG_CASE, d.CASE_COMPLETED, {"at": ctx.now()}, actor)

        return self._exec(idem_key, body, handler)

    # ============ 查询（只读） ============

    def case_view(self, case_id: str) -> dict:
        case = d.fold(self.store.load_stream(case_id), AGG_CASE)
        if case is None:
            raise NotFoundError(f"项目不存在: {case_id}")
        milestones = []
        for mid in case.milestone_ids:
            m = d.fold(self.store.load_stream(mid), AGG_MILESTONE)
            milestones.append(self._milestone_dict(m))
        return {
            "case_id": case.case_id, "code": case.code, "track": case.track,
            "applicant": case.applicant, "applicant_org": case.applicant_org,
            "requested_cents": case.requested_cents,
            "status": case.status,
            "application_versions": case.versions,
            "rounds": [self._round_dict(case.rounds[n]) for n in sorted(case.rounds)],
            "current_round_no": case.current_round_no,
            "decision": case.decision,
            "conditions_satisfied": case.conditions_satisfied,
            "appeal": case.appeal,
            "contract": case.contract,
            "failure_flag": case.failure_flag,
            "failure_waiver": case.failure_waiver,
            "termination": case.termination,
            "milestones": milestones,
            "version": case.version,
        }

    @staticmethod
    def _round_dict(r: d.RoundState) -> dict:
        return {
            "round_no": r.round_no, "status": r.status,
            "assignments": r.assignments, "excluded": r.excluded,
            "assignment_note": r.assignment_note,
            "scores": list(r.scores.values()),
            "abstentions": [
                {"reviewer_id": k, "reason": v} for k, v in sorted(r.abstentions.items())
            ],
            "questions": r.questions,
            "opened_at": r.opened_at, "closed_at": r.closed_at,
        }

    @staticmethod
    def _milestone_dict(m: d.MilestoneState) -> dict:
        return {
            "milestone_id": m.milestone_id, "case_id": m.case_id,
            "seq": m.seq, "name": m.name, "description": m.description,
            "due_date": m.due_date, "amount_cents": m.amount_cents,
            "criteria": m.criteria, "status": m.status,
            "evidence": m.evidence, "technical": m.technical, "financial": m.financial,
            "frozen_at": m.frozen_at, "payment_ref": m.payment_ref,
            "paid_at": m.paid_at, "clawback": m.clawback, "version": m.version,
        }

    def milestone_view(self, milestone_id: str) -> dict:
        m = d.fold(self.store.load_stream(milestone_id), AGG_MILESTONE)
        if m is None:
            raise NotFoundError(f"里程碑不存在: {milestone_id}")
        return self._milestone_dict(m)

    def fund_view(self, fund_id: str) -> dict:
        fund = d.fold(self.store.load_stream(fund_id), AGG_FUND)
        if fund is None:
            raise NotFoundError(f"基金不存在: {fund_id}")
        snap = ledger_snapshot(project_entries(self.store.load_all()), fund_id)
        return {
            "fund_id": fund.fund_id, "code": fund.code, "name": fund.name,
            "total_cents": fund.total_cents, "sources": fund.sources,
            "available_cents": snap.available_cents,
            "committed_cents": snap.committed_cents,
            "frozen_cents": snap.frozen_cents,
            "paid_cents": snap.paid_cents,
            "by_source": snap.by_source,
            "by_case": snap.by_case,
        }

    def fund_position(self, fund_id: str, as_of: str | None = None) -> dict:
        """管理层按任一日期查看承诺/已付/冻结/可用及来源（ISO 日期或时间戳）。"""
        if d.fold(self.store.load_stream(fund_id), AGG_FUND) is None:
            raise NotFoundError(f"基金不存在: {fund_id}")
        entries = project_entries(self.store.load_all())
        if as_of is not None and len(as_of) == 10:
            as_of = as_of + "T23:59:59.999999+00:00"
        snap = ledger_snapshot(entries, fund_id, as_of)
        return asdict(snap)

    def audit_trail(self, aggregate_id: str) -> list[dict]:
        events = self.store.load_stream(aggregate_id)
        if not events:
            raise NotFoundError(f"聚合不存在或无事件: {aggregate_id}")
        return [
            {"seq": e.seq, "version": e.version, "type": e.event_type,
             "payload": e.payload, "actor": e.actor, "idem_key": e.idem_key,
             "at": e.recorded_at, "prev_hash": e.prev_hash, "hash": e.hash}
            for e in events
        ]

    def verify_integrity(self) -> None:
        self.store.verify_chain()
