"""应用服务：评审、决策、合同里程碑、拨款与资金守恒的全部业务规则。

每个公共方法都在单个串行化事务内完成状态变更、复式分录与哈希链审计，
因此崩溃、并发或重试都不会产生重复投票、重复付款或金额泄漏。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict
from typing import Any

from .clock import Clock
from .contracts import Money, canonical_fingerprint
from .domain import (
    AppealState,
    Assignment,
    AssignmentStatus,
    Ballot,
    CaseState,
    DecisionOutcome,
    MilestoneStatus,
)
from .errors import ConflictError, FundError, NotFoundError, StateError, ValidationError
from .inputs import parse_content, parse_money
from . import ledger
from .repository import Repository, dumps, loads, require_slug

MIN_REVIEWERS = 3

SCORE_WEIGHTS: dict[str, dict[str, float]] = {
    "tech_transfer": {"tech": 0.40, "team": 0.25, "usage": 0.20, "risk": 0.15},
    "component": {"tech": 0.35, "team": 0.20, "usage": 0.15, "market": 0.20, "risk": 0.10},
    "scenario_ops": {"tech": 0.20, "team": 0.20, "usage": 0.20, "market": 0.25, "risk": 0.15},
}


class FundService:
    def __init__(self, repo: Repository, clock: Clock | None = None) -> None:
        self.repo = repo
        self.clock = clock or Clock()

    # ================================================================ 基础数据
    def register_reviewer(self, reviewer_id: str, name: str, expertise: list[str],
                          affiliations: list[str], related_party_keys: list[str]) -> dict[str, Any]:
        require_slug(reviewer_id, "评委ID")
        if not name.strip():
            raise ValidationError("评委姓名不能为空")
        now = self.clock.iso()
        with self.repo.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM reviewers WHERE reviewer_id=?", (reviewer_id,)).fetchone()
            if exists:
                raise ConflictError(f"评委已存在: {reviewer_id}")
            conn.execute(
                "INSERT INTO reviewers (reviewer_id, name, expertise_json, affiliations_json,"
                " related_keys_json, active) VALUES (?,?,?,?,?,1)",
                (reviewer_id, name.strip(), dumps(list(expertise)),
                 dumps([f"org:{a.strip()}" for a in affiliations]),
                 dumps(sorted(set(related_party_keys)))),
            )
            self._audit(conn, actor=reviewer_id, action="reviewer_registered",
                        aggregate="reviewer", aggregate_id=reviewer_id,
                        payload={"name": name, "expertise": expertise}, now=now)
        return {"reviewer_id": reviewer_id, "registered_at": now}

    def add_fund_source(self, source: str, name: str, amount: Any) -> dict[str, Any]:
        require_slug(source, "资金来源")
        money = parse_money(amount, "出资额")
        if money.cents <= 0:
            raise ValidationError("出资额必须大于零")
        now = self.clock.iso()
        tx_id = ledger.new_tx_id()
        with self.repo.transaction() as conn:
            if conn.execute("SELECT 1 FROM fund_sources WHERE source=?", (source,)).fetchone():
                raise ConflictError(f"资金来源已存在: {source}")
            conn.execute(
                "INSERT INTO fund_sources (source, name, cents, currency, added_at)"
                " VALUES (?,?,?,?,?)",
                (source, name, money.cents, money.currency, now),
            )
            ledger.post(
                conn, tx_id=tx_id, tx_type="capital_in", posted_at=now,
                legs=[(f"available:{source}", money.cents, 0),
                      (f"capital:{source}", 0, money.cents)],
                currency=money.currency, case_code=None, ref_id=source,
                memo=f"{name} 出资",
            )
            self._audit(conn, actor="system", action="fund_source_added",
                        aggregate="fund", aggregate_id=source,
                        payload={"amount": money.to_dict()}, now=now, tx_id=tx_id)
        return {"source": source, "amount": money.to_dict(), "added_at": now}

    # ================================================================ 申请版本
    def create_case(self, case_code: str, applicant: str, round_name: str) -> dict[str, Any]:
        require_slug(case_code, "项目编号")
        if not applicant.strip() or not round_name.strip():
            raise ValidationError("申请人与评审轮次不能为空")
        now = self.clock.iso()
        with self.repo.transaction() as conn:
            if conn.execute("SELECT 1 FROM cases WHERE case_code=?", (case_code,)).fetchone():
                raise ConflictError(f"项目已存在: {case_code}")
            conn.execute(
                "INSERT INTO cases (case_code, applicant, round_name, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (case_code, applicant.strip(), round_name.strip(), CaseState.DRAFT, now, now),
            )
            self._audit(conn, actor=applicant, action="case_created",
                        aggregate="case", aggregate_id=case_code,
                        payload={"round_name": round_name}, now=now)
        return {"case_code": case_code, "state": CaseState.DRAFT, "created_at": now}

    def submit_application(self, case_code: str, data: dict[str, Any], submitted_by: str) -> dict[str, Any]:
        content = parse_content(data)
        fp = content.fingerprint()
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            if case["state"] not in (CaseState.DRAFT, CaseState.IN_REVIEW):
                raise StateError(f"项目状态 {case['state']} 下不能提交材料")
            latest = conn.execute(
                "SELECT version FROM app_versions WHERE case_code=? ORDER BY version DESC LIMIT 1",
                (case_code,)).fetchone()
            if latest:
                last_fp = conn.execute(
                    "SELECT fingerprint FROM app_versions WHERE case_code=? AND version=?",
                    (case_code, latest["version"])).fetchone()["fingerprint"]
                if last_fp == fp:
                    return {"case_code": case_code, "version": latest["version"],
                            "deduplicated": True, "fingerprint": fp}
            version = 1 if latest is None else latest["version"] + 1
            parent = None if latest is None else latest["version"]
            now = self.clock.iso()
            conn.execute(
                "INSERT INTO app_versions (case_code, version, fingerprint, content_json,"
                " submitted_by, submitted_at, parent_version) VALUES (?,?,?,?,?,?,?)",
                (case_code, version, fp, dumps(_content_json(content)),
                 submitted_by, now, parent),
            )
            conn.execute("UPDATE cases SET updated_at=? WHERE case_code=?", (now, case_code))
            self._audit(conn, actor=submitted_by, action="application_submitted",
                        aggregate="case", aggregate_id=case_code,
                        payload={"version": version, "fingerprint": fp}, now=now)
            opened_round = None
            if case["state"] == CaseState.IN_REVIEW:
                opened_round = self._open_new_round(conn, case_code, now)
                self._rerun_conflicts(conn, case_code, content, now)
            result = {"case_code": case_code, "version": version, "fingerprint": fp,
                      "parent_version": parent, "submitted_at": now}
            if opened_round:
                result["new_round"] = opened_round
            return result

    def list_versions(self, case_code: str) -> list[dict[str, Any]]:
        with self.repo.transaction() as conn:
            self._case(conn, case_code)
            rows = conn.execute(
                "SELECT version, fingerprint, submitted_by, submitted_at, parent_version,"
                " content_json FROM app_versions WHERE case_code=? ORDER BY version",
                (case_code,)).fetchall()
            return [{
                "version": r["version"], "fingerprint": r["fingerprint"],
                "submitted_by": r["submitted_by"], "submitted_at": r["submitted_at"],
                "parent_version": r["parent_version"],
                "content": _hydrate_content(loads(r["content_json"])),
            } for r in rows]

    def lock_for_review(self, case_code: str) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            if case["state"] != CaseState.DRAFT:
                raise StateError("只有草案可以锁定送评")
            latest = self._latest_version_row(conn, case_code)
            now = self.clock.iso()
            conn.execute("UPDATE cases SET state=?, updated_at=? WHERE case_code=?",
                         (CaseState.IN_REVIEW, now, case_code))
            conn.execute("INSERT INTO rounds (case_code, round_version, opened_at) VALUES (?,1,?)",
                         (case_code, now))
            self._audit(conn, actor="system", action="case_locked_for_review",
                        aggregate="case", aggregate_id=case_code,
                        payload={"application_version": latest["version"]}, now=now)
            return {"case_code": case_code, "state": CaseState.IN_REVIEW,
                    "round_version": 1, "application_version": latest["version"]}

    # ================================================================ 回避与分配
    def assign_reviewers(self, case_code: str, reviewer_ids: list[str] | None = None) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            if case["state"] != CaseState.IN_REVIEW:
                raise StateError("只有在评项目可以分配评委")
            content = self._latest_content(conn, case_code)
            if reviewer_ids is None:
                reviewer_ids = [r["reviewer_id"] for r in conn.execute(
                    "SELECT reviewer_id FROM reviewers WHERE active=1 ORDER BY reviewer_id")]
            if not reviewer_ids:
                raise ValidationError("未指定任何评委")
            now = self.clock.iso()
            results = []
            for reviewer_id in reviewer_ids:
                reviewer = conn.execute(
                    "SELECT * FROM reviewers WHERE reviewer_id=? AND active=1",
                    (reviewer_id,)).fetchone()
                if reviewer is None:
                    raise NotFoundError(f"评委不存在或已停用: {reviewer_id}")
                existed = conn.execute(
                    "SELECT 1 FROM assignments WHERE case_code=? AND reviewer_id=?",
                    (case_code, reviewer_id)).fetchone()
                if existed:
                    raise ConflictError(f"评委 {reviewer_id} 已分配到该项目")
                checks, eligible, reason = self._conflict_check(content, reviewer)
                conn.execute(
                    "INSERT INTO assignments (case_code, reviewer_id, assigned_at,"
                    " conflict_json, eligible, reason, status) VALUES (?,?,?,?,?,?,?)",
                    (case_code, reviewer_id, now, dumps(checks), 1 if eligible else 0,
                     reason, AssignmentStatus.ASSIGNED),
                )
                results.append({"reviewer_id": reviewer_id, "eligible": eligible, "reason": reason,
                                "checks": checks})
            eligible_ids = [r["reviewer_id"] for r in self._eligible_assignments(conn, case_code)]
            if len(eligible_ids) < MIN_REVIEWERS:
                raise StateError(
                    f"合资格评委仅 {len(eligible_ids)} 人，少于 {MIN_REVIEWERS} 人，"
                    f"请补充无利益冲突的评委: {eligible_ids}")
            self._audit(conn, actor="system", action="reviewers_assigned",
                        aggregate="case", aggregate_id=case_code,
                        payload={"results": results}, now=now)
            return {"case_code": case_code, "assignments": results,
                    "eligible_reviewer_ids": eligible_ids}

    def _conflict_check(self, content: Any, reviewer: sqlite3.Row) -> tuple[list[dict[str, Any]], bool, str]:
        affiliations = set(loads(reviewer["affiliations_json"]))
        related = set(loads(reviewer["related_keys_json"]))
        checks: list[dict[str, Any]] = []
        conflicts: list[str] = []
        for party in content.related_parties:
            key = party.key()
            matched_basis: list[str] = []
            if key in affiliations:
                matched_basis.append("任职机构")
            if key in related:
                matched_basis.append("申报利益关系")
            checks.append({"target": key, "relation": party.relation,
                           "matched": bool(matched_basis), "basis": matched_basis})
            if matched_basis:
                conflicts.append(f"{key}（{party.relation}）")
        for member in content.team:
            person_key = f"person:{str(member.get('name', '')).strip()}"
            org = str(member.get("org", "")).strip()
            org_key = f"org:{org}" if org else ""
            basis = []
            if person_key in related or person_key == f"person:{reviewer['name']}":
                basis.append("团队成员本人")
            if org_key and org_key in affiliations:
                basis.append("团队成员任职机构")
            if basis:
                checks.append({"target": person_key, "relation": "团队成员",
                               "matched": True, "basis": basis})
                conflicts.append(f"{person_key}（团队成员）")
        if conflicts:
            return checks, False, "触发回避: " + "、".join(conflicts)
        return checks, True, "未发现与申报关联方或团队的利益关系"

    def _rerun_conflicts(self, conn: sqlite3.Connection, case_code: str,
                         content: Any, now: str) -> None:
        for row in conn.execute(
                "SELECT a.*, r.* FROM assignments a JOIN reviewers r"
                " ON a.reviewer_id=r.reviewer_id WHERE a.case_code=? AND a.eligible=1",
                (case_code,)).fetchall():
            _, eligible, reason = self._conflict_check(content, row)
            if not eligible:
                conn.execute(
                    "UPDATE assignments SET status=?, reason=? WHERE case_code=? AND reviewer_id=?",
                    (AssignmentStatus.RECUSED, f"材料修订后{reason}", case_code, row["reviewer_id"]))
                self._audit(conn, actor="system", action="reviewer_recused",
                            aggregate="case", aggregate_id=case_code,
                            payload={"reviewer_id": row["reviewer_id"], "reason": reason}, now=now)

    # ================================================================ 评分质询投票
    def submit_score(self, case_code: str, reviewer_id: str, dimensions: dict[str, int],
                     rationale: str) -> dict[str, Any]:
        if not rationale.strip():
            raise ValidationError("评分必须给出理由")
        with self.repo.transaction() as conn:
            self._require_eligible(conn, case_code, reviewer_id)
            content = self._latest_content(conn, case_code)
            weights = SCORE_WEIGHTS[content.kind]
            if set(dimensions) != set(weights):
                raise ValidationError(f"评分维度必须恰好为 {sorted(weights)}")
            for dim, value in dimensions.items():
                if not isinstance(value, int) or not 0 <= value <= 100:
                    raise ValidationError(f"维度 {dim} 得分必须为 0..100 整数")
            round_version = self._current_round(conn, case_code)
            now = self.clock.iso()
            try:
                conn.execute(
                    "INSERT INTO scorecards (case_code, reviewer_id, round_version,"
                    " dimensions_json, rationale, scored_at) VALUES (?,?,?,?,?,?)",
                    (case_code, reviewer_id, round_version, dumps(dimensions), rationale, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该评委在本轮已提交评分") from exc
            total = round(sum(dimensions[d] * w for d, w in weights.items()), 4)
            self._audit(conn, actor=reviewer_id, action="score_submitted",
                        aggregate="case", aggregate_id=case_code,
                        payload={"round": round_version, "dimensions": dimensions,
                                 "weighted_total": total}, now=now)
            return {"case_code": case_code, "reviewer_id": reviewer_id,
                    "round_version": round_version, "weighted_total": total, "scored_at": now}

    def ask_question(self, case_code: str, reviewer_id: str, content_text: str) -> dict[str, Any]:
        if not content_text.strip():
            raise ValidationError("质询内容不能为空")
        with self.repo.transaction() as conn:
            self._require_eligible(conn, case_code, reviewer_id)
            qid = uuid.uuid4().hex
            now = self.clock.iso()
            conn.execute(
                "INSERT INTO questions (question_id, case_code, reviewer_id, asked_at, content)"
                " VALUES (?,?,?,?,?)",
                (qid, case_code, reviewer_id, now, content_text.strip()),
            )
            self._audit(conn, actor=reviewer_id, action="question_asked",
                        aggregate="case", aggregate_id=case_code,
                        payload={"question_id": qid}, now=now)
            return {"question_id": qid, "asked_at": now}

    def respond_question(self, question_id: str, response: str, responded_by: str) -> dict[str, Any]:
        if not response.strip():
            raise ValidationError("答复内容不能为空")
        with self.repo.transaction() as conn:
            q = conn.execute("SELECT * FROM questions WHERE question_id=?", (question_id,)).fetchone()
            if q is None:
                raise NotFoundError(f"质询不存在: {question_id}")
            if q["response"] is not None:
                raise ConflictError("该质询已答复，答复不可改写")
            latest = self._latest_version_row(conn, q["case_code"])
            now = self.clock.iso()
            conn.execute(
                "UPDATE questions SET response=?, responded_at=?, response_version=?"
                " WHERE question_id=?",
                (response.strip(), now, latest["version"], question_id),
            )
            self._audit(conn, actor=responded_by, action="question_answered",
                        aggregate="case", aggregate_id=q["case_code"],
                        payload={"question_id": question_id,
                                 "application_version": latest["version"]}, now=now)
            return {"question_id": question_id, "responded_at": now,
                    "application_version": latest["version"]}

    def cast_ballot(self, case_code: str, reviewer_id: str, vote: str, comment: str) -> dict[str, Any]:
        if vote not in {o.value for o in DecisionOutcome}:
            raise ValidationError(f"投票意向非法: {vote}")
        with self.repo.transaction() as conn:
            self._require_eligible(conn, case_code, reviewer_id)
            round_version = self._current_round(conn, case_code)
            now = self.clock.iso()
            try:
                conn.execute(
                    "INSERT INTO ballots (case_code, reviewer_id, round_version, vote, comment, cast_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (case_code, reviewer_id, round_version, vote, comment.strip(), now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该评委在本轮已投票，投票不可更改或重复") from exc
            self._audit(conn, actor=reviewer_id, action="ballot_cast",
                        aggregate="case", aggregate_id=case_code,
                        payload={"round": round_version, "vote": vote}, now=now)
            return {"case_code": case_code, "reviewer_id": reviewer_id,
                    "round_version": round_version, "vote": vote, "cast_at": now}

    # ================================================================ 决策
    def decide(self, case_code: str, decided_by: str, approved_amount: Any = None,
               conditions: list[dict[str, Any]] | None = None,
               rationale: str = "") -> dict[str, Any]:
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            if case["state"] != CaseState.IN_REVIEW:
                raise StateError("只有在评项目可以作出决策")
            round_version = self._current_round(conn, case_code)
            eligible = self._eligible_assignments(conn, case_code)
            eligible_ids = [r["reviewer_id"] for r in eligible]
            if len(eligible_ids) < MIN_REVIEWERS:
                raise StateError(
                    f"合资格评委仅 {len(eligible_ids)} 人，少于 {MIN_REVIEWERS} 人，"
                    "材料修订触发回避后须补足评委才能决策")
            ballots = conn.execute(
                "SELECT * FROM ballots WHERE case_code=? AND round_version=? ORDER BY reviewer_id",
                (case_code, round_version)).fetchall()
            voted = {b["reviewer_id"] for b in ballots}
            missing = sorted(set(eligible_ids) - voted)
            if missing:
                raise StateError(f"合资格评委尚未完成本轮投票: {missing}")
            scores = conn.execute(
                "SELECT * FROM scorecards WHERE case_code=? AND round_version=?",
                (case_code, round_version)).fetchall()
            scored = {s["reviewer_id"] for s in scores}
            if set(eligible_ids) - scored:
                raise StateError("所有投票评委必须先提交带理由的评分")
            tally = {o.value: 0 for o in DecisionOutcome}
            for b in ballots:
                tally[b["vote"]] += 1
            winner = max(tally, key=lambda k: tally[k])
            if list(tally.values()).count(tally[winner]) > 1:
                raise StateError(f"投票平票 {tally}，须补充评议后重新表决")
            outcome = winner
            content = self._latest_content(conn, case_code)
            money = content.requested_amount if approved_amount is None else parse_money(approved_amount, "核准金额")
            if outcome == DecisionOutcome.REJECT:
                money = Money(0, content.requested_amount.currency)
            elif money.cents <= 0 or money > content.requested_amount:
                raise ValidationError("核准金额必须为正且不超过申请金额")
            parsed_conditions = self._parse_conditions(conditions or [], outcome)
            weights = SCORE_WEIGHTS[content.kind]
            breakdown = self._score_breakdown(scores, weights, tally, eligible_ids)
            decision_version = self._next_decision_version(conn, case_code)
            now = self.clock.iso()
            conn.execute(
                "INSERT INTO decisions (case_code, decision_version, outcome, approved_cents,"
                " currency, score_json, conditions_json, rationale, decided_by, decided_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (case_code, decision_version, outcome, money.cents, money.currency,
                 dumps(breakdown), dumps([asdict(c) for c in parsed_conditions]),
                 rationale.strip(), decided_by, now),
            )
            conn.execute("UPDATE cases SET state=?, updated_at=? WHERE case_code=?",
                         (CaseState.DECIDED, now, case_code))
            conn.execute("UPDATE rounds SET closed_at=? WHERE case_code=? AND round_version=?",
                         (now, case_code, round_version))
            tx_id = None
            allocations: list[dict[str, Any]] = []
            if money.cents:
                tx_id = ledger.new_tx_id()
                available = ledger.available_by_source(conn)
                alloc = ledger.pick_sources(dict(available), money.cents)
                legs: list[tuple[str, int, int]] = []
                for source, cents in alloc:
                    legs.append((f"committed:{case_code}:{source}", cents, 0))
                    legs.append((f"available:{source}", 0, cents))
                    conn.execute(
                        "INSERT INTO case_allocations (case_code, source, cents) VALUES (?,?,?)",
                        (case_code, source, cents),
                    )
                    allocations.append({"source": source, "cents": cents})
                ledger.post(conn, tx_id=tx_id, tx_type="commit", posted_at=now, legs=legs,
                            currency=money.currency, case_code=case_code,
                            ref_id=f"decision:{decision_version}", memo="投资决策承诺")
            self._audit(conn, actor=decided_by, action="decision_made",
                        aggregate="case", aggregate_id=case_code,
                        payload={"decision_version": decision_version, "outcome": outcome,
                                 "approved": money.to_dict(), "tally": tally,
                                 "conditions": [asdict(c) for c in parsed_conditions],
                                 "allocations": allocations, "score": breakdown},
                        now=now, tx_id=tx_id)
            return {"case_code": case_code, "decision_version": decision_version,
                    "outcome": outcome, "approved_amount": money.to_dict(),
                    "tally": tally, "conditions": [asdict(c) for c in parsed_conditions],
                    "score_breakdown": breakdown, "allocations": allocations,
                    "decided_at": now}

    def _parse_conditions(self, raw: list[dict[str, Any]], outcome: str) -> list[Any]:
        from .domain import Condition
        conditions = []
        for index, item in enumerate(raw, start=1):
            desc = str(item.get("description", "")).strip()
            if not desc:
                raise ValidationError("条件描述不能为空")
            seq = item.get("milestone_seq")
            if seq is not None and (not isinstance(seq, int) or seq < 1):
                raise ValidationError("条件绑定的里程碑序号必须为正整数")
            conditions.append(Condition(
                condition_id=f"c{index}", description=desc, milestone_seq=seq))
        if outcome == DecisionOutcome.CONDITIONAL and not conditions:
            raise ValidationError("有条件通过必须附带至少一条前置条件")
        return conditions

    def _score_breakdown(self, scores: list[sqlite3.Row], weights: dict[str, float],
                         tally: dict[str, int], eligible_ids: list[str]) -> dict[str, Any]:
        per_reviewer = {}
        dim_acc: dict[str, list[int]] = {}
        for row in scores:
            dims = loads(row["dimensions_json"])
            total = round(sum(dims[d] * w for d, w in weights.items()), 4)
            per_reviewer[row["reviewer_id"]] = {
                "dimensions": dims, "weighted_total": total,
                "rationale": row["rationale"],
            }
            for dim, value in dims.items():
                dim_acc.setdefault(dim, []).append(value)
        averages = {dim: round(sum(values) / len(values), 4) for dim, values in sorted(dim_acc.items())}
        overall = round(sum(averages[d] * w for d, w in weights.items()), 4)
        return {"weights": weights, "per_reviewer": per_reviewer,
                "dimension_averages": averages, "overall": overall,
                "tally": tally, "panel": eligible_ids}

    # ================================================================ 申诉
    def file_appeal(self, case_code: str, grounds: str, filed_by: str) -> dict[str, Any]:
        if not grounds.strip():
            raise ValidationError("申诉理由不能为空")
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            if case["state"] != CaseState.DECIDED:
                raise StateError("仅已决策未签约项目可以申诉")
            decision = conn.execute(
                "SELECT * FROM decisions WHERE case_code=? AND void=0 ORDER BY decision_version DESC",
                (case_code,)).fetchone()
            if decision is None:
                raise StateError("没有有效的决策可供申诉")
            if conn.execute("SELECT 1 FROM appeals WHERE case_code=? AND state=?",
                            (case_code, AppealState.FILED)).fetchone():
                raise ConflictError("该项目已有待裁定申诉")
            appeal_id = uuid.uuid4().hex
            now = self.clock.iso()
            conn.execute(
                "INSERT INTO appeals (appeal_id, case_code, against_decision_version, filed_by,"
                " filed_at, grounds, state) VALUES (?,?,?,?,?,?,?)",
                (appeal_id, case_code, decision["decision_version"], filed_by, now,
                 grounds.strip(), AppealState.FILED),
            )
            self._audit(conn, actor=filed_by, action="appeal_filed",
                        aggregate="appeal", aggregate_id=appeal_id,
                        payload={"case_code": case_code,
                                 "against": decision["decision_version"]}, now=now)
            return {"appeal_id": appeal_id, "state": AppealState.FILED, "filed_at": now}

    def rule_appeal(self, appeal_id: str, uphold: bool, ruling: str, reviewed_by: str) -> dict[str, Any]:
        if not ruling.strip():
            raise ValidationError("裁定意见不能为空")
        with self.repo.transaction() as conn:
            appeal = conn.execute("SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
            if appeal is None:
                raise NotFoundError(f"申诉不存在: {appeal_id}")
            if appeal["state"] != AppealState.FILED:
                raise ConflictError("申诉已裁定，不可改写")
            now = self.clock.iso()
            new_state = AppealState.UPHELD if uphold else AppealState.REJECTED
            conn.execute(
                "UPDATE appeals SET state=?, reviewed_by=?, reviewed_at=?, ruling=? WHERE appeal_id=?",
                (new_state, reviewed_by, now, ruling.strip(), appeal_id),
            )
            tx_id = None
            if uphold:
                conn.execute("UPDATE decisions SET void=1 WHERE case_code=? AND decision_version=?",
                             (appeal["case_code"], appeal["against_decision_version"]))
                conn.execute("UPDATE cases SET state=?, updated_at=? WHERE case_code=?",
                             (CaseState.IN_REVIEW, now, appeal["case_code"]))
                max_round = conn.execute(
                    "SELECT COALESCE(MAX(round_version),0) AS v FROM rounds WHERE case_code=?",
                    (appeal["case_code"],)).fetchone()["v"]
                conn.execute(
                    "INSERT INTO rounds (case_code, round_version, opened_at) VALUES (?,?,?)",
                    (appeal["case_code"], max_round + 1, now),
                )
                tx_id = self._reverse_commitment(
                    conn, appeal["case_code"], "commitment_release", now,
                    f"申诉 {appeal_id} 成立，撤销承诺", ref=appeal_id)
            self._audit(conn, actor=reviewed_by, action="appeal_ruled",
                        aggregate="appeal", aggregate_id=appeal_id,
                        payload={"state": new_state, "uphold": uphold}, now=now, tx_id=tx_id)
            return {"appeal_id": appeal_id, "state": new_state, "ruled_at": now}

    # ================================================================ 合同与里程碑
    def sign_contract(self, case_code: str, milestones: list[dict[str, Any]],
                      signed_by: str) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            if case["state"] != CaseState.DECIDED:
                raise StateError("只有已决策项目可以签订合同")
            decision = conn.execute(
                "SELECT * FROM decisions WHERE case_code=? AND void=0 ORDER BY decision_version DESC",
                (case_code,)).fetchone()
            if decision is None or decision["outcome"] == DecisionOutcome.REJECT:
                raise StateError("否决项目不能签订合同")
            if not milestones:
                raise ValidationError("合同至少包含一个里程碑")
            seqs = [m.get("seq") for m in milestones]
            if sorted(seqs) != list(range(1, len(milestones) + 1)):
                raise ValidationError("里程碑序号必须从 1 开始且连续唯一")
            total = 0
            built: list[dict[str, Any]] = []
            for m in milestones:
                name = str(m.get("name", "")).strip()
                criteria = str(m.get("criteria", "")).strip()
                if not name or not criteria:
                    raise ValidationError("里程碑名称与验收标准不能为空")
                amount = parse_money(m.get("amount"), "里程碑金额")
                if amount.cents <= 0:
                    raise ValidationError("里程碑金额必须为正")
                total += amount.cents
                built.append({"seq": m["seq"], "name": name, "criteria": criteria,
                              "amount": amount})
            if total != decision["approved_cents"]:
                raise ValidationError(
                    f"里程碑金额合计必须等于核定额: {total} != {decision['approved_cents']}（分）")
            conditions = loads(decision["conditions_json"])
            now = self.clock.iso()
            contract_id = f"{case_code}-contract"
            conn.execute(
                "INSERT INTO contracts (contract_id, case_code, decision_version, total_cents,"
                " currency, signed_at) VALUES (?,?,?,?,?,?)",
                (contract_id, case_code, decision["decision_version"], total,
                 decision["currency"], now),
            )
            for m in built:
                milestone_id = f"{case_code}-m{m['seq']}"
                required = [c["condition_id"] for c in conditions
                            if c["milestone_seq"] is None or c["milestone_seq"] == m["seq"]]
                conn.execute(
                    "INSERT INTO milestones (milestone_id, case_code, seq, name, criteria,"
                    " amount_cents, currency, status, required_conditions_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (milestone_id, case_code, m["seq"], m["name"], m["criteria"],
                     m["amount"].cents, decision["currency"], MilestoneStatus.PLANNED,
                     dumps(required)),
                )
            conn.execute("UPDATE cases SET state=?, updated_at=? WHERE case_code=?",
                         (CaseState.CONTRACTED, now, case_code))
            self._audit(conn, actor=signed_by, action="contract_signed",
                        aggregate="contract", aggregate_id=contract_id,
                        payload={"total_cents": total, "milestones": len(built)}, now=now)
            return {"contract_id": contract_id, "total_cents": total,
                    "milestones": [{"seq": m["seq"], "amount": m["amount"].to_dict()} for m in built],
                    "signed_at": now}

    def clear_condition(self, case_code: str, condition_id: str, cleared_by: str) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            decision = conn.execute(
                "SELECT * FROM decisions WHERE case_code=? AND void=0 ORDER BY decision_version DESC",
                (case_code,)).fetchone()
            condition_ids = {c["condition_id"] for c in loads(decision["conditions_json"])}
            if condition_id not in condition_ids:
                raise NotFoundError(f"条件不存在: {condition_id}")
            now = self.clock.iso()
            try:
                conn.execute(
                    "INSERT INTO condition_clearances (case_code, condition_id, cleared_by, cleared_at)"
                    " VALUES (?,?,?,?)",
                    (case_code, condition_id, cleared_by, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该条件已确认满足") from exc
            self._audit(conn, actor=cleared_by, action="condition_cleared",
                        aggregate="case", aggregate_id=case_code,
                        payload={"condition_id": condition_id}, now=now)
            return {"case_code": case_code, "condition_id": condition_id, "cleared_at": now}

    def submit_evidence(self, case_code: str, seq: int, evidence_ref: str, submitted_by: str) -> dict[str, Any]:
        if not evidence_ref.strip():
            raise ValidationError("证据引用不能为空")
        with self.repo.transaction() as conn:
            milestone = self._milestone(conn, case_code, seq)
            if milestone["status"] != MilestoneStatus.PLANNED:
                raise StateError(f"里程碑状态 {milestone['status']} 下不能提交证据")
            now = self.clock.iso()
            conn.execute(
                "UPDATE milestones SET status=?, evidence_ref=?, evidence_submitted_at=? WHERE milestone_id=?",
                (MilestoneStatus.EVIDENCE_SUBMITTED, evidence_ref.strip(), now, milestone["milestone_id"]),
            )
            self._event(conn, milestone, "evidence_submitted", submitted_by, now,
                        {"evidence_ref": evidence_ref})
            self._audit(conn, actor=submitted_by, action="evidence_submitted",
                        aggregate="milestone", aggregate_id=milestone["milestone_id"],
                        payload={"evidence_ref": evidence_ref}, now=now)
            return {"milestone_id": milestone["milestone_id"],
                    "status": MilestoneStatus.EVIDENCE_SUBMITTED, "submitted_at": now}

    def tech_review(self, case_code: str, seq: int, approved: bool, review_ref: str,
                    reviewer: str, note: str = "") -> dict[str, Any]:
        if not review_ref.strip():
            raise ValidationError("技术复核意见引用不能为空")
        with self.repo.transaction() as conn:
            milestone = self._milestone(conn, case_code, seq)
            if milestone["status"] != MilestoneStatus.EVIDENCE_SUBMITTED:
                raise StateError(f"里程碑状态 {milestone['status']} 下不能做技术复核")
            now = self.clock.iso()
            if not approved:
                conn.execute(
                    "UPDATE milestones SET status=?, tech_review_ref=?, tech_reviewer=?,"
                    " tech_reviewed_at=? WHERE milestone_id=?",
                    (MilestoneStatus.FAILED, review_ref.strip(), reviewer, now,
                     milestone["milestone_id"]),
                )
                self._event(conn, milestone, "tech_failed", reviewer, now, {"note": note})
                self._audit(conn, actor=reviewer, action="milestone_failed",
                            aggregate="milestone", aggregate_id=milestone["milestone_id"],
                            payload={"review_ref": review_ref, "note": note}, now=now)
                self._terminate_locked(conn, case_code, reviewer,
                                       f"里程碑 {milestone['milestone_id']} 技术验收失败: {note}",
                                       milestone_failure=True, now=now)
                return {"milestone_id": milestone["milestone_id"],
                        "status": MilestoneStatus.FAILED, "terminated": True, "reviewed_at": now}
            conn.execute(
                "UPDATE milestones SET status=?, tech_review_ref=?, tech_reviewer=?,"
                " tech_reviewed_at=? WHERE milestone_id=?",
                (MilestoneStatus.TECH_APPROVED, review_ref.strip(), reviewer, now,
                 milestone["milestone_id"]),
            )
            self._event(conn, milestone, "tech_approved", reviewer, now, {"review_ref": review_ref})
            self._audit(conn, actor=reviewer, action="tech_review_passed",
                        aggregate="milestone", aggregate_id=milestone["milestone_id"],
                        payload={"review_ref": review_ref}, now=now)
            return {"milestone_id": milestone["milestone_id"],
                    "status": MilestoneStatus.TECH_APPROVED, "reviewed_at": now}

    def finance_review(self, case_code: str, seq: int, approved: bool, review_ref: str,
                       reviewer: str, note: str = "") -> dict[str, Any]:
        if not review_ref.strip():
            raise ValidationError("财务复核意见引用不能为空")
        with self.repo.transaction() as conn:
            milestone = self._milestone(conn, case_code, seq)
            if milestone["status"] not in (MilestoneStatus.TECH_APPROVED, MilestoneStatus.BLOCKED):
                raise StateError(f"里程碑状态 {milestone['status']} 下不能做财务复核")
            now = self.clock.iso()
            if not approved:
                conn.execute(
                    "UPDATE milestones SET status=?, finance_review_ref=?, finance_reviewer=?,"
                    " finance_reviewed_at=? WHERE milestone_id=?",
                    (MilestoneStatus.BLOCKED, review_ref.strip(), reviewer, now,
                     milestone["milestone_id"]),
                )
                self._event(conn, milestone, "finance_blocked", reviewer, now, {"note": note})
                self._audit(conn, actor=reviewer, action="finance_review_blocked",
                            aggregate="milestone", aggregate_id=milestone["milestone_id"],
                            payload={"note": note}, now=now)
                return {"milestone_id": milestone["milestone_id"],
                        "status": MilestoneStatus.BLOCKED, "reviewed_at": now}
            uncleared = self._uncleared_conditions(conn, case_code, milestone)
            if uncleared:
                raise StateError(f"前置条件未满足，里程碑冻结: {uncleared}")
            conn.execute(
                "UPDATE milestones SET status=?, finance_review_ref=?, finance_reviewer=?,"
                " finance_reviewed_at=? WHERE milestone_id=?",
                (MilestoneStatus.FINANCE_APPROVED, review_ref.strip(), reviewer, now,
                 milestone["milestone_id"]),
            )
            self._event(conn, milestone, "finance_approved", reviewer, now, {"review_ref": review_ref})
            self._audit(conn, actor=reviewer, action="finance_review_passed",
                        aggregate="milestone", aggregate_id=milestone["milestone_id"],
                        payload={"review_ref": review_ref}, now=now)
            return {"milestone_id": milestone["milestone_id"],
                    "status": MilestoneStatus.FINANCE_APPROVED, "reviewed_at": now}

    # ================================================================ 拨款
    def disburse(self, case_code: str, seq: int, idempotency_key: str,
                 requested_by: str, reference: str | None = None) -> dict[str, Any]:
        if not idempotency_key.strip():
            raise ValidationError("幂等键不能为空")
        with self.repo.transaction() as conn:
            cached = conn.execute(
                "SELECT result_json FROM idempotency WHERE idem_key=?",
                (f"disburse:{idempotency_key}",)).fetchone()
            if cached is not None:
                result = loads(cached["result_json"])
                if result["case_code"] != case_code or result["seq"] != seq:
                    raise ConflictError("幂等键已用于其他里程碑，拒绝支付")
                result["replayed"] = True
                return result
            milestone = self._milestone(conn, case_code, seq)
            if milestone["status"] == MilestoneStatus.RELEASED:
                raise ConflictError("里程碑已拨款，请勿重复支付")
            if milestone["status"] != MilestoneStatus.FINANCE_APPROVED:
                raise StateError(f"里程碑状态 {milestone['status']}，未通过双复核不能拨款")
            uncleared = self._uncleared_conditions(conn, case_code, milestone)
            if uncleared:
                raise StateError(f"前置条件未满足，拒绝拨款: {uncleared}")
            remaining = ledger.case_balance_by_source(conn, "committed", case_code)
            allocations = ledger.pick_sources(dict(remaining), milestone["amount_cents"])
            now = self.clock.iso()
            tx_id = ledger.new_tx_id()
            legs: list[tuple[str, int, int]] = []
            disbursement_id = f"{case_code}-m{seq}-pay"
            for source, cents in allocations:
                legs.append((f"paid:{case_code}:{source}", cents, 0))
                legs.append((f"committed:{case_code}:{source}", 0, cents))
                conn.execute(
                    "INSERT INTO disbursements (disbursement_id, idempotency_key, case_code,"
                    " milestone_id, amount_cents, currency, status, requested_at, paid_at,"
                    " reference, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (disbursement_id, idempotency_key, case_code, milestone["milestone_id"],
                     cents, milestone["currency"], "paid", now, now, reference, source),
                )
            ledger.post(conn, tx_id=tx_id, tx_type="disburse", posted_at=now, legs=legs,
                        currency=milestone["currency"], case_code=case_code,
                        ref_id=disbursement_id, memo=f"里程碑 {seq} 拨款")
            conn.execute(
                "UPDATE milestones SET status=? WHERE milestone_id=?",
                (MilestoneStatus.RELEASED, milestone["milestone_id"]),
            )
            self._event(conn, milestone, "released", requested_by, now,
                        {"idempotency_key": idempotency_key, "allocations": allocations})
            result = {"case_code": case_code, "seq": seq,
                      "disbursement_id": disbursement_id,
                      "idempotency_key": idempotency_key,
                      "amount_cents": milestone["amount_cents"],
                      "currency": milestone["currency"], "status": "paid",
                      "allocations": [{"source": s, "cents": c} for s, c in allocations],
                      "paid_at": now, "replayed": False}
            conn.execute(
                "INSERT INTO idempotency (idem_key, result_json, created_at) VALUES (?,?,?)",
                (f"disburse:{idempotency_key}", dumps(result), now),
            )
            self._audit(conn, actor=requested_by, action="disbursed",
                        aggregate="disbursement", aggregate_id=disbursement_id,
                        payload={"key": idempotency_key, "allocations": allocations,
                                 "amount_cents": milestone["amount_cents"]},
                        now=now, tx_id=tx_id)
            return result

    def recall_disbursement(self, case_code: str, seq: int, reason: str,
                            recalled_by: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("撤回原因不能为空")
        with self.repo.transaction() as conn:
            milestone = self._milestone(conn, case_code, seq)
            disbursement_id = f"{case_code}-m{seq}-pay"
            rows = conn.execute(
                "SELECT * FROM disbursements WHERE disbursement_id=? ORDER BY source",
                (disbursement_id,)).fetchall()
            if not rows:
                raise NotFoundError("该里程碑没有拨款记录")
            if any(r["status"] != "paid" for r in rows):
                raise ConflictError("拨款已撤回，不能重复撤回")
            now = self.clock.iso()
            tx_id = ledger.new_tx_id()
            recall_id = f"{disbursement_id}-recall"
            legs: list[tuple[str, int, int]] = []
            total = 0
            for r in rows:
                legs.append((f"frozen:{case_code}:{r['source']}", r["amount_cents"], 0))
                legs.append((f"paid:{case_code}:{r['source']}", 0, r["amount_cents"]))
                conn.execute(
                    "INSERT INTO recalls (recall_id, disbursement_id, case_code, amount_cents,"
                    " currency, reason, recalled_at, recalled_by, source)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (recall_id, disbursement_id, case_code, r["amount_cents"], r["currency"],
                     reason.strip(), now, recalled_by, r["source"]),
                )
                total += r["amount_cents"]
            conn.execute("UPDATE disbursements SET status='recalled' WHERE disbursement_id=?",
                         (disbursement_id,))
            ledger.post(conn, tx_id=tx_id, tx_type="recall", posted_at=now, legs=legs,
                        currency=rows[0]["currency"], case_code=case_code,
                        ref_id=recall_id, memo=f"撤回拨款: {reason}")
            self._event(conn, milestone, "recalled", recalled_by, now,
                        {"reason": reason, "amount_cents": total})
            self._audit(conn, actor=recalled_by, action="disbursement_recalled",
                        aggregate="disbursement", aggregate_id=disbursement_id,
                        payload={"recall_id": recall_id, "reason": reason,
                                 "amount_cents": total}, now=now, tx_id=tx_id)
            return {"recall_id": recall_id, "disbursement_id": disbursement_id,
                    "amount_cents": total, "recalled_at": now}

    # ================================================================ 终止与解冻
    def terminate(self, case_code: str, reason: str, terminated_by: str,
                  milestone_failure: bool = False) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("终止原因不能为空")
        with self.repo.transaction() as conn:
            now = self.clock.iso()
            result = self._terminate_locked(conn, case_code, terminated_by, reason,
                                            milestone_failure, now)
            return result

    def _terminate_locked(self, conn: sqlite3.Connection, case_code: str, actor: str,
                          reason: str, milestone_failure: bool, now: str) -> dict[str, Any]:
        case = self._case(conn, case_code)
        if case["state"] == CaseState.TERMINATED:
            raise StateError("项目已终止")
        cancelled = 0
        milestone_rows = conn.execute(
            "SELECT * FROM milestones WHERE case_code=? ORDER BY seq", (case_code,)).fetchall()
        for m in milestone_rows:
            if m["status"] == MilestoneStatus.RELEASED:
                continue
            if m["status"] == MilestoneStatus.FAILED:
                # 技术失败的里程碑保留 failed 状态，其额度同样计入取消。
                cancelled += m["amount_cents"]
                continue
            if m["status"] == MilestoneStatus.CANCELLED:
                continue
            conn.execute("UPDATE milestones SET status=? WHERE milestone_id=?",
                         (MilestoneStatus.CANCELLED, m["milestone_id"]))
            self._event(conn, m, "cancelled", actor, now, {"reason": reason})
            cancelled += m["amount_cents"]
        remaining_committed = ledger.case_balance_by_source(conn, "committed", case_code)
        committed_total = sum(remaining_committed.values())
        if not milestone_rows:
            # 已决策但尚未签订合同：全部剩余承诺都随终止取消。
            cancelled = committed_total
        if committed_total != cancelled:
            raise StateError(
                f"待取消额度 {cancelled} 与剩余承诺 {committed_total} 不一致，终止已拒绝")
        tx_id = None
        if committed_total:
            tx_id = self._move_committed_to_frozen(conn, case_code, remaining_committed, now,
                                                   f"项目终止: {reason}")
        conn.execute(
            "INSERT INTO terminations (case_code, terminated_at, terminated_by, reason,"
            " milestone_failure, unreleased_cancelled_cents, currency) VALUES (?,?,?,?,?,?,?)",
            (case_code, now, actor, reason.strip(), 1 if milestone_failure else 0,
             cancelled, "CNY"),
        )
        conn.execute("UPDATE cases SET state=?, updated_at=? WHERE case_code=?",
                     (CaseState.TERMINATED, now, case_code))
        self._audit(conn, actor=actor, action="case_terminated",
                    aggregate="case", aggregate_id=case_code,
                    payload={"reason": reason, "milestone_failure": milestone_failure,
                             "cancelled_cents": cancelled}, now=now, tx_id=tx_id)
        return {"case_code": case_code, "state": CaseState.TERMINATED,
                "cancelled_cents": cancelled, "milestone_failure": milestone_failure,
                "terminated_at": now}

    def defrost(self, case_code: str, actor: str, memo: str = "争议结清，冻结资金回收") -> dict[str, Any]:
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            if case["state"] != CaseState.TERMINATED:
                raise StateError("只有已终止项目可以解冻回收资金")
            frozen = ledger.case_balance_by_source(conn, "frozen", case_code)
            if not frozen or sum(frozen.values()) <= 0:
                raise ConflictError("该项目没有可解冻的冻结资金")
            now = self.clock.iso()
            tx_id = ledger.new_tx_id()
            legs: list[tuple[str, int, int]] = []
            for source, cents in frozen.items():
                if cents <= 0:
                    continue
                legs.append((f"available:{source}", cents, 0))
                legs.append((f"frozen:{case_code}:{source}", 0, cents))
            ledger.post(conn, tx_id=tx_id, tx_type="defrost", posted_at=now, legs=legs,
                        currency="CNY", case_code=case_code, ref_id=case_code, memo=memo)
            self._audit(conn, actor=actor, action="frozen_defrosted",
                        aggregate="case", aggregate_id=case_code,
                        payload={"frozen": frozen, "memo": memo}, now=now, tx_id=tx_id)
            return {"case_code": case_code, "returned": frozen, "defrosted_at": now}

    # ================================================================ 查询
    def get_case(self, case_code: str) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            case = self._case(conn, case_code)
            versions = conn.execute(
                "SELECT version, fingerprint, submitted_by, submitted_at, parent_version,"
                " content_json FROM app_versions WHERE case_code=? ORDER BY version",
                (case_code,)).fetchall()
            assignments = conn.execute(
                "SELECT * FROM assignments WHERE case_code=? ORDER BY reviewer_id",
                (case_code,)).fetchall()
            questions = conn.execute(
                "SELECT * FROM questions WHERE case_code=? ORDER BY asked_at",
                (case_code,)).fetchall()
            ballots = conn.execute(
                "SELECT * FROM ballots WHERE case_code=? ORDER BY round_version, reviewer_id",
                (case_code,)).fetchall()
            scores = conn.execute(
                "SELECT * FROM scorecards WHERE case_code=? ORDER BY round_version, reviewer_id",
                (case_code,)).fetchall()
            decisions = conn.execute(
                "SELECT * FROM decisions WHERE case_code=? ORDER BY decision_version",
                (case_code,)).fetchall()
            appeals = conn.execute("SELECT * FROM appeals WHERE case_code=? ORDER BY filed_at",
                                   (case_code,)).fetchall()
            contract = conn.execute("SELECT * FROM contracts WHERE case_code=?",
                                    (case_code,)).fetchone()
            milestones = conn.execute(
                "SELECT * FROM milestones WHERE case_code=? ORDER BY seq", (case_code,)).fetchall()
            disbursements = conn.execute(
                "SELECT * FROM disbursements WHERE case_code=? ORDER BY milestone_id, source",
                (case_code,)).fetchall()
            recalls = conn.execute(
                "SELECT * FROM recalls WHERE case_code=? ORDER BY recalled_at",
                (case_code,)).fetchall()
            termination = conn.execute("SELECT * FROM terminations WHERE case_code=?",
                                       (case_code,)).fetchone()
            return {
                "case_code": case_code, "applicant": case["applicant"],
                "round_name": case["round_name"], "state": case["state"],
                "created_at": case["created_at"], "updated_at": case["updated_at"],
                "versions": [{
                    "version": v["version"], "fingerprint": v["fingerprint"],
                    "submitted_by": v["submitted_by"], "submitted_at": v["submitted_at"],
                    "parent_version": v["parent_version"],
                    "content": _hydrate_content(loads(v["content_json"])),
                } for v in versions],
                "assignments": [dict(a) | {"conflict_checks": loads(a["conflict_json"])}
                                for a in assignments],
                "questions": [dict(q) for q in questions],
                "scores": [{"reviewer_id": s["reviewer_id"], "round_version": s["round_version"],
                            "dimensions": loads(s["dimensions_json"]),
                            "rationale": s["rationale"], "scored_at": s["scored_at"]}
                           for s in scores],
                "ballots": [dict(b) for b in ballots],
                "decisions": [{
                    "decision_version": d["decision_version"], "outcome": d["outcome"],
                    "approved_cents": d["approved_cents"], "void": bool(d["void"]),
                    "rationale": d["rationale"], "decided_by": d["decided_by"],
                    "decided_at": d["decided_at"],
                    "conditions": loads(d["conditions_json"]),
                    "score_breakdown": loads(d["score_json"]),
                } for d in decisions],
                "appeals": [dict(a) for a in appeals],
                "contract": None if contract is None else {
                    "contract_id": contract["contract_id"],
                    "decision_version": contract["decision_version"],
                    "total_cents": contract["total_cents"], "signed_at": contract["signed_at"],
                },
                "milestones": [self._milestone_dict(m, disbursements, recalls) for m in milestones],
                "termination": None if termination is None else dict(termination),
            }

    def fund_snapshot(self, as_of: str | None = None) -> dict[str, Any]:
        with self.repo.transaction() as conn:
            cutoff = as_of or self.clock.iso()
            # 仅给日期时按当日最后一刻处理，词法比较即可覆盖全天事件。
            if len(cutoff) == 10:
                cutoff = f"{cutoff}T23:59:59.999999+00:00"
            data = ledger.snapshot(conn, cutoff)
            data["as_of"] = as_of or cutoff
            return _money_json(data)

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.repo.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def verify(self) -> dict[str, Any]:
        """对外一致性自检：哈希链 + 复式守恒。"""
        with self.repo.transaction() as conn:
            chain_ok = self.repo.verify_chain_integrity()
            ledger.verify_conservation(conn)
            row = conn.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            from .domain import AuditRecord
            tail = ""
            if row is not None:
                tail = AuditRecord(
                    seq=row["seq"], occurred_at=row["occurred_at"], actor=row["actor"],
                    action=row["action"], aggregate=row["aggregate"],
                    aggregate_id=row["aggregate_id"],
                    payload_fingerprint=row["payload_fingerprint"],
                    prev_hash=row["prev_hash"], tx_id=row["tx_id"]).hash()
            return {"audit_chain_intact": chain_ok, "ledger_conserved": True,
                    "audit_tail": tail}

    # ================================================================ 内部助手
    def _case(self, conn: sqlite3.Connection, case_code: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM cases WHERE case_code=?", (case_code,)).fetchone()
        if row is None:
            raise NotFoundError(f"项目不存在: {case_code}")
        return row

    def _latest_version_row(self, conn: sqlite3.Connection, case_code: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM app_versions WHERE case_code=? ORDER BY version DESC LIMIT 1",
            (case_code,)).fetchone()
        if row is None:
            raise StateError("项目尚未提交任何材料版本")
        return row

    def _latest_content(self, conn: sqlite3.Connection, case_code: str) -> Any:
        row = self._latest_version_row(conn, case_code)
        return _content_from_json(loads(row["content_json"]))

    def _eligible_assignments(self, conn: sqlite3.Connection, case_code: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM assignments WHERE case_code=? AND eligible=1 AND status=?",
            (case_code, AssignmentStatus.ASSIGNED)).fetchall()

    def _require_eligible(self, conn: sqlite3.Connection, case_code: str, reviewer_id: str) -> None:
        case = self._case(conn, case_code)
        if case["state"] != CaseState.IN_REVIEW:
            raise StateError("项目当前不在评审中")
        row = conn.execute(
            "SELECT * FROM assignments WHERE case_code=? AND reviewer_id=?",
            (case_code, reviewer_id)).fetchone()
        if row is None:
            raise NotFoundError("该评委未被分配到此项目")
        if not row["eligible"]:
            raise StateError(f"评委存在利益冲突，依回避规则不得参与: {row['reason']}")
        if row["status"] != AssignmentStatus.ASSIGNED:
            raise StateError(f"评委已被{row['status']}，不得继续参与本轮")

    def _current_round(self, conn: sqlite3.Connection, case_code: str) -> int:
        row = conn.execute(
            "SELECT round_version FROM rounds WHERE case_code=? AND closed_at IS NULL"
            " ORDER BY round_version DESC LIMIT 1", (case_code,)).fetchone()
        if row is None:
            raise StateError("评审轮次尚未开启")
        return row["round_version"]

    def _open_new_round(self, conn: sqlite3.Connection, case_code: str, now: str) -> int:
        previous = conn.execute(
            "SELECT MAX(round_version) AS v FROM rounds WHERE case_code=?", (case_code,)).fetchone()
        new_round = (previous["v"] or 0) + 1
        conn.execute("UPDATE rounds SET closed_at=? WHERE case_code=? AND closed_at IS NULL",
                     (now, case_code))
        conn.execute("INSERT INTO rounds (case_code, round_version, opened_at) VALUES (?,?,?)",
                     (case_code, new_round, now))
        return new_round

    def _milestone(self, conn: sqlite3.Connection, case_code: str, seq: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM milestones WHERE case_code=? AND seq=?",
                           (case_code, seq)).fetchone()
        if row is None:
            raise NotFoundError(f"里程碑不存在: {case_code}# {seq}")
        return row

    def _uncleared_conditions(self, conn: sqlite3.Connection, case_code: str,
                              milestone: sqlite3.Row) -> list[str]:
        required = loads(milestone["required_conditions_json"])
        if not required:
            return []
        placeholders = ",".join("?" for _ in required)
        cleared = {r["condition_id"] for r in conn.execute(
            f"SELECT condition_id FROM condition_clearances WHERE case_code=? "
            f"AND condition_id IN ({placeholders})", [case_code, *required])}
        return sorted(set(required) - cleared)

    def _next_decision_version(self, conn: sqlite3.Connection, case_code: str) -> int:
        row = conn.execute("SELECT COALESCE(MAX(decision_version),0) AS v FROM decisions"
                           " WHERE case_code=?", (case_code,)).fetchone()
        return row["v"] + 1

    def _reverse_commitment(self, conn: sqlite3.Connection, case_code: str, tx_type: str,
                            now: str, memo: str, ref: str) -> str | None:
        remaining = ledger.case_balance_by_source(conn, "committed", case_code)
        if not remaining or sum(remaining.values()) <= 0:
            return None
        tx_id = ledger.new_tx_id()
        legs: list[tuple[str, int, int]] = []
        for source, cents in remaining.items():
            if cents <= 0:
                continue
            legs.append((f"available:{source}", cents, 0))
            legs.append((f"committed:{case_code}:{source}", 0, cents))
        ledger.post(conn, tx_id=tx_id, tx_type=tx_type, posted_at=now, legs=legs,
                    currency="CNY", case_code=case_code, ref_id=ref, memo=memo)
        conn.execute("DELETE FROM case_allocations WHERE case_code=?", (case_code,))
        return tx_id

    def _move_committed_to_frozen(self, conn: sqlite3.Connection, case_code: str,
                                  remaining: dict[str, int], now: str, memo: str) -> str:
        tx_id = ledger.new_tx_id()
        legs: list[tuple[str, int, int]] = []
        for source, cents in remaining.items():
            if cents <= 0:
                continue
            legs.append((f"frozen:{case_code}:{source}", cents, 0))
            legs.append((f"committed:{case_code}:{source}", 0, cents))
        ledger.post(conn, tx_id=tx_id, tx_type="commitment_freeze", posted_at=now, legs=legs,
                    currency="CNY", case_code=case_code, ref_id=case_code, memo=memo)
        conn.execute("DELETE FROM case_allocations WHERE case_code=?", (case_code,))
        return tx_id

    def _event(self, conn: sqlite3.Connection, milestone: sqlite3.Row, event_type: str,
               actor: str, now: str, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO milestone_events (milestone_id, case_code, event_type, actor,"
            " occurred_at, data_json) VALUES (?,?,?,?,?,?)",
            (milestone["milestone_id"], milestone["case_code"], event_type, actor, now,
             dumps(data)),
        )

    def _milestone_dict(self, m: sqlite3.Row, disbursements: list[sqlite3.Row],
                        recalls: list[sqlite3.Row]) -> dict[str, Any]:
        paid = [r for r in disbursements if r["milestone_id"] == m["milestone_id"]]
        recall_rows = [r for r in recalls
                       if any(r["disbursement_id"] == p["disbursement_id"] for p in paid)]
        return {
            "milestone_id": m["milestone_id"], "seq": m["seq"], "name": m["name"],
            "criteria": m["criteria"], "amount_cents": m["amount_cents"],
            "currency": m["currency"], "status": m["status"],
            "required_conditions": loads(m["required_conditions_json"]),
            "evidence_ref": m["evidence_ref"],
            "evidence_submitted_at": m["evidence_submitted_at"],
            "tech_review_ref": m["tech_review_ref"], "tech_reviewer": m["tech_reviewer"],
            "tech_reviewed_at": m["tech_reviewed_at"],
            "finance_review_ref": m["finance_review_ref"],
            "finance_reviewer": m["finance_reviewer"],
            "finance_reviewed_at": m["finance_reviewed_at"],
            "disbursements": [{"source": r["source"], "amount_cents": r["amount_cents"],
                               "status": r["status"], "paid_at": r["paid_at"],
                               "idempotency_key": r["idempotency_key"]} for r in paid],
            "recalls": [{"source": r["source"], "amount_cents": r["amount_cents"],
                         "reason": r["reason"], "recalled_at": r["recalled_at"],
                         "recalled_by": r["recalled_by"]} for r in recall_rows],
        }

    def _audit(self, conn: sqlite3.Connection, *, actor: str, action: str, aggregate: str,
               aggregate_id: str, payload: dict[str, Any], now: str,
               tx_id: str | None = None) -> None:
        self.repo.append_audit(
            conn, actor=actor, action=action, aggregate=aggregate, aggregate_id=aggregate_id,
            payload_fingerprint=canonical_fingerprint(payload), tx_id=tx_id, occurred_at=now)


# ---------------------------------------------------------------- 序列化助手
def _content_json(content: Any) -> dict[str, Any]:
    return {
        "project_name": content.project_name,
        "kind": content.kind,
        "tech_route": content.tech_route,
        "team": content.team,
        "fund_usage": content.fund_usage,
        "related_parties": [asdict(p) for p in content.related_parties],
        "requested_amount": content.requested_amount.to_dict(),
    }


def _content_from_json(data: dict[str, Any]) -> Any:
    from .domain import ApplicationContent, RelatedParty
    return ApplicationContent(
        project_name=data["project_name"], kind=data["kind"], tech_route=data["tech_route"],
        team=data["team"], fund_usage=data["fund_usage"],
        related_parties=[RelatedParty(**p) for p in data["related_parties"]],
        requested_amount=Money(data["requested_amount"]["cents"],
                               data["requested_amount"]["currency"]),
    )


def _hydrate_content(data: dict[str, Any]) -> dict[str, Any]:
    return data | {"requested_amount_yuan": data["requested_amount"]["cents"] / 100}


def _money_json(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, Money):
            out[key] = {"cents": value.cents, "currency": value.currency,
                        "yuan": value.cents / 100}
        else:
            out[key] = value
    return out
