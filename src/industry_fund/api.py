"""HTTP 接口（标准库 http.server，零第三方依赖）。

路由约定见 README。要点：
- 所有写操作接受 JSON 请求体，可用 `Idempotency-Key` 请求头实现命令级幂等；
- 操作人可通过 `X-Actor` 请求头或 body.actor 提供；
- 领域错误映射为 4xx，成功返回 201 与新事件清单。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import (
    ConflictOfInterestError, ConflictStateError, DomainError,
    IdempotencyConflict, NotFoundError, ValidationError,
)
from .events import EventStore
from .money import to_yuan
from .service import FundService

_JSON = "application/json; charset=utf-8"
_CENT_KEYS = ("total_cents", "available_cents", "committed_cents",
              "frozen_cents", "paid_cents")


def _money_view(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k.endswith("_cents"):
            out[k[:-6]] = to_yuan(v)
        else:
            out[k] = v
    return out


def create_handler(store: EventStore) -> type[BaseHTTPRequestHandler]:
    service = FundService(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "IndustryFund/1.0"

        def log_message(self, fmt, *args):  # 静默，避免污染输出
            return

        # ---------- 基础 ----------

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", _JSON)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def _error(self, exc: DomainError) -> None:
            if isinstance(exc, NotFoundError):
                code, label = 404, "not_found"
            elif isinstance(exc, ConflictOfInterestError):
                code, label = 403, "conflict_of_interest"
            elif isinstance(exc, IdempotencyConflict):
                code, label = 409, "idempotency_conflict"
            elif isinstance(exc, ConflictStateError):
                code, label = 409, "illegal_state"
            elif isinstance(exc, ValidationError):
                code, label = 422, "validation_error"
            else:
                code, label = 400, "domain_error"
            self._send_json(code, {"error": label, "message": str(exc)})

        # ---------- 查询 ----------

        def do_GET(self) -> None:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            qs = parse_qs(urlsplit(self.path).query)
            try:
                if len(parts) == 2 and parts[0] == "funds":
                    self._send_json(200, self._fund_view(parts[1], qs))
                elif len(parts) == 2 and parts[0] == "cases":
                    self._send_json(200, service.case_view(parts[1]))
                elif len(parts) == 2 and parts[0] == "milestones":
                    self._send_json(200, service.milestone_view(parts[1]))
                elif len(parts) == 3 and parts[0] == "aggregates" and parts[2] == "audit":
                    self._send_json(200, {"events": service.audit_trail(parts[1])})
                else:
                    self._send_json(404, {"error": "not_found", "message": "未知路径"})
            except DomainError as exc:
                self._error(exc)

        def _fund_view(self, fund_id: str, qs: dict) -> dict:
            if "as_of" not in qs:
                view = service.fund_view(fund_id)
                head = {
                    "fund_id": view["fund_id"], "code": view["code"], "name": view["name"],
                    "total": to_yuan(view["total_cents"]),
                    "available": to_yuan(view["available_cents"]),
                    "committed": to_yuan(view["committed_cents"]),
                    "frozen": to_yuan(view["frozen_cents"]),
                    "paid": to_yuan(view["paid_cents"]),
                }
                return {
                    **head,
                    "sources": [_money_view(r) for r in view["sources"]],
                    "by_source": [_money_view(r) for r in view["by_source"]],
                    "by_case": [_money_view(r) for r in view["by_case"]],
                }
            data = service.fund_position(fund_id, as_of=qs["as_of"][0])
            out = {k: v for k, v in data.items()
                   if k not in ("by_source", "by_case")}
            for k in _CENT_KEYS:
                out[k[:-6]] = to_yuan(out.pop(k))
            out["by_source"] = [_money_view(r) for r in data["by_source"]]
            out["by_case"] = [_money_view(r) for r in data["by_case"]]
            return out

        # ---------- 命令 ----------

        def do_POST(self) -> None:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            try:
                body = self._read_body()
                idem = self.headers.get("Idempotency-Key") or body.pop("idem_key", None)
                actor = (body.pop("actor", None) or self.headers.get("X-Actor")
                         or "api-client")
                try:
                    result = self._dispatch(parts, body, str(actor), idem)
                except (KeyError, TypeError) as exc:
                    self._send_json(422, {"error": "validation_error",
                                          "message": f"请求缺少或含有非法字段: {exc}"})
                    return
                self._send_json(201, result if result is not None else {"ok": True})
            except DomainError as exc:
                self._error(exc)

        def _dispatch(self, parts: list[str], body: dict,
                      actor: str, idem: str | None) -> dict:
            s = service

            if parts == ["funds"]:
                return s.establish_fund(
                    body["fund_id"], body["code"], body["name"], body["sources"],
                    actor=actor, idem_key=idem)
            if parts == ["reviewers"]:
                return s.register_reviewer(
                    body["reviewer_id"], body["name"], body.get("org", ""),
                    body.get("expertise", []), idem_key=idem)
            if parts == ["cases"]:
                return s.create_case(
                    body["case_id"], body["code"], body["track"], body["applicant"],
                    body.get("applicant_org", ""), body["requested_amount"],
                    actor=actor, idem_key=idem)
            if parts == ["admin", "verify-integrity"]:
                s.verify_integrity()
                return {"ok": True, "message": "审计链校验通过"}

            if len(parts) >= 2:
                kind, rid = parts[0], parts[1]
                action = parts[2] if len(parts) == 3 else None
                if kind == "reviewers":
                    if action == "deactivate":
                        return s.deactivate_reviewer(rid, body.get("reason", ""),
                                                     actor=actor, idem_key=idem)
                    if action == "relationships":
                        return s.declare_relationship(
                            rid, body["related_party"], body["relation_type"],
                            body.get("detail", ""), idem_key=idem)
                if kind == "cases":
                    return self._case_dispatch(rid, action, body, actor, idem)
                if kind == "milestones":
                    return self._milestone_dispatch(rid, action, body, actor, idem)

            raise NotFoundError(f"未知路径: /{'/'.join(parts)}")

        def _case_dispatch(self, case_id, action, body, actor, idem) -> dict:
            s = service
            if action == "applications":
                return s.submit_application(
                    case_id, body["technical_route"], body["team"],
                    body["use_of_funds"], body["related_parties"],
                    actor=actor, version_label=body.get("version_label", ""),
                    documents=body.get("documents"),
                    change_summary=body.get("change_summary", ""), idem_key=idem)
            if action == "rounds":
                return s.open_review_round(case_id, actor=actor, idem_key=idem)
            if action == "assignments":
                return s.assign_reviewers(
                    case_id, actor=actor, reviewer_ids=body.get("reviewer_ids"),
                    min_reviewers=int(body.get("min_reviewers", 3)), idem_key=idem)
            if action == "scores":
                return s.submit_score(
                    case_id, body["reviewer_id"], body["scores"],
                    body.get("comment", ""), idem_key=idem)
            if action == "abstentions":
                return s.abstain_score(
                    case_id, body["reviewer_id"], body["reason"], idem_key=idem)
            if action == "questions":
                return s.ask_question(
                    case_id, body["reviewer_id"], body["content"], idem_key=idem)
            if action == "answers":
                return s.answer_question(
                    case_id, body["question_id"], body["content"],
                    actor=actor, idem_key=idem)
            if action == "close-round":
                return s.close_round(case_id, actor=actor, idem_key=idem)
            if action == "decision":
                return s.make_decision(
                    case_id, body["result"], body["rationale"], actor,
                    conditions=body.get("conditions"),
                    approve_line=float(body.get("approve_line", 75.0)),
                    conditional_line=float(body.get("conditional_line", 60.0)),
                    idem_key=idem)
            if action == "conditions":
                return s.satisfy_conditions(
                    case_id, body.get("note", ""), actor, idem_key=idem)
            if action == "appeals":
                return s.appeal_decision(
                    case_id, body["reason"], actor,
                    evidence=body.get("evidence"), idem_key=idem)
            if action == "appeal-ruling":
                return s.rule_appeal(
                    case_id, body["ruling"], body["note"], actor, idem_key=idem)
            if action == "contract":
                return s.sign_contract(
                    case_id, body["fund_id"], body["milestones"],
                    actor=actor, idem_key=idem)
            if action == "failure-flags":
                return s.flag_failure(
                    case_id, body["reason"], actor,
                    evidence=body.get("evidence"), idem_key=idem)
            if action == "failure-waivers":
                return s.waive_failure(
                    case_id, body["reason"], actor, idem_key=idem)
            if action == "termination":
                return s.terminate_case(
                    case_id, body["reason"], actor, idem_key=idem)
            if action == "completion":
                return s.complete_case(case_id, actor=actor, idem_key=idem)
            if action == "allocation-releases":
                return s.release_allocation(
                    body["fund_id"], body["amount"], body["reason"], actor,
                    case_id=case_id, milestone_id=body.get("milestone_id"),
                    from_account=body.get("from_account", "committed"), idem_key=idem)
            raise NotFoundError(f"未知项目操作: {action}")

        def _milestone_dispatch(self, milestone_id, action, body, actor, idem) -> dict:
            s = service
            if action == "evidence":
                return s.submit_evidence(
                    milestone_id, body["files"], body["summary"],
                    actor=actor, idem_key=idem)
            if action == "technical-review":
                return s.review_technical(
                    milestone_id, bool(body["passed"]), body["opinion"],
                    body.get("reviewer", actor), idem_key=idem)
            if action == "financial-review":
                return s.review_financial(
                    milestone_id, bool(body["passed"]), body["opinion"],
                    body.get("reviewer", actor),
                    eligible_amount=body.get("eligible_amount"), idem_key=idem)
            if action == "payment":
                return s.release_payment(
                    milestone_id, body["payment_ref"], actor=actor, idem_key=idem)
            if action == "clawback":
                return s.clawback_payment(
                    milestone_id, body["reason"], actor,
                    amount=body.get("amount"), idem_key=idem)
            raise NotFoundError(f"未知里程碑操作: {action}")

    return Handler


def run_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    store = EventStore(db_path)
    handler = create_handler(store)
    httpd = ThreadingHTTPServer((host, port), handler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        store.close()
