"""基于标准库 http.server 的 JSON API，无需第三方依赖。

所有写接口接受 JSON body；重复请求可携带 ``Idempotency-Key`` 头，
服务端对同一请求体返回同一结果（拨款另有业务级幂等键）。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Any, Callable

from .errors import FundError
from .repository import Repository, dumps, loads
from .service import FundService


def build_service(path: str = ":memory:") -> FundService:
    return FundService(Repository(path))


class ApiHandler(BaseHTTPRequestHandler):
    service: FundService  # 由工厂注入类属性

    server_version = "IndustryFundAPI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._error(400, "bad_json", f"请求体不是合法 JSON: {exc}")
            raise _Handled()
        if not isinstance(data, dict):
            self._error(400, "bad_json", "请求体必须是 JSON 对象")
            raise _Handled()
        return data

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"error": {"code": code, "message": message}})

    def _call(self, fn_name: str, **kwargs: Any) -> None:
        fn: Callable[..., Any] = getattr(self.service, fn_name)
        idem = self.headers.get("Idempotency-Key")
        cache_key = f"api:{idem}" if idem else None
        try:
            if cache_key:
                with self.service.repo.transaction() as conn:
                    cached = conn.execute(
                        "SELECT result_json FROM idempotency WHERE idem_key=?",
                        (cache_key,)).fetchone()
                if cached is not None:
                    payload = loads(cached["result_json"])
                    payload["replayed"] = True
                    self._send(200, payload)
                    return
            result = fn(**kwargs)
            if cache_key:
                with self.service.repo.transaction() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO idempotency (idem_key, result_json, created_at)"
                        " VALUES (?,?,?)",
                        (cache_key, dumps(result), self.service.clock.iso()),
                    )
            self._send(200, result)
        except FundError as exc:
            status = {
                "validation": 400, "state": 409, "conflict": 409,
                "not_found": 404, "conservation": 422,
            }.get(exc.code, 400)
            self._error(status, exc.code, str(exc))
        except TypeError as exc:
            self._error(400, "bad_request", f"请求参数不匹配: {exc}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        try:
            if len(parts) == 2 and parts[0] == "cases":
                self._send(200, self.service.get_case(parts[1]))
            elif parts == ["fund", "snapshot"]:
                as_of = query.get("as_of", [None])[0]
                self._send(200, self.service.fund_snapshot(as_of))
            elif parts == ["audit"]:
                self._send(200, {"records": self.service.audit_log(500),
                                 "verification": self.service.verify()})
            elif parts == ["health"]:
                self._send(200, {"status": "ok"})
            else:
                self._error(404, "not_found", f"未知路径: {self.path}")
        except FundError as exc:
            self._error(404 if exc.code == "not_found" else 400, exc.code, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            data = self._read_json()
            try:
                self._route_post(parts, data)
            except FundError as exc:
                status = {
                    "validation": 400, "state": 409, "conflict": 409,
                    "not_found": 404, "conservation": 422,
                }.get(exc.code, 400)
                self._error(status, exc.code, str(exc))
            except KeyError as exc:
                self._error(400, "missing_field", f"请求缺少字段: {exc.args[0]}")
            except Exception as exc:  # noqa: BLE001
                self._error(500, "internal", f"服务器内部错误: {exc}")
        except _Handled:
            return

    def _route_post(self, parts: list[str], d: dict[str, Any]) -> None:
        p = parts
        if p == ["reviewers"]:
            self._call("register_reviewer", reviewer_id=d["reviewer_id"], name=d["name"],
                       expertise=d.get("expertise", []),
                       affiliations=d.get("affiliations", []),
                       related_party_keys=d.get("related_party_keys", []))
        elif p == ["fund", "sources"]:
            self._call("add_fund_source", source=d["source"], name=d["name"],
                       amount=d["amount"])
        elif p == ["cases"]:
            self._call("create_case", case_code=d["case_code"], applicant=d["applicant"],
                       round_name=d["round_name"])
        elif len(p) == 3 and p[0] == "cases" and p[2] == "applications":
            self._call("submit_application", case_code=p[1], data=d,
                       submitted_by=d.get("submitted_by", "applicant"))
        elif len(p) == 3 and p[2] == "lock":
            self._call("lock_for_review", case_code=p[1])
        elif len(p) == 3 and p[2] == "assignments":
            self._call("assign_reviewers", case_code=p[1],
                       reviewer_ids=d.get("reviewer_ids"))
        elif len(p) == 4 and p[2] == "scores":
            self._call("submit_score", case_code=p[1], reviewer_id=p[3],
                       dimensions=d["dimensions"], rationale=d["rationale"])
        elif len(p) == 3 and p[2] == "questions":
            self._call("ask_question", case_code=p[1], reviewer_id=d["reviewer_id"],
                       content_text=d["content"])
        elif len(p) == 4 and p[2] == "questions":
            self._call("respond_question", question_id=p[3], response=d["response"],
                       responded_by=d["responded_by"])
        elif len(p) == 4 and p[2] == "ballots":
            self._call("cast_ballot", case_code=p[1], reviewer_id=p[3],
                       vote=d["vote"], comment=d.get("comment", ""))
        elif len(p) == 3 and p[2] == "decision":
            self._call("decide", case_code=p[1], decided_by=d["decided_by"],
                       approved_amount=d.get("approved_amount"),
                       conditions=d.get("conditions"), rationale=d.get("rationale", ""))
        elif len(p) == 3 and p[2] == "appeals":
            self._call("file_appeal", case_code=p[1], grounds=d["grounds"],
                       filed_by=d["filed_by"])
        elif len(p) == 3 and p[0] == "appeals" and p[2] == "ruling":
            self._call("rule_appeal", appeal_id=p[1], uphold=bool(d["uphold"]),
                       ruling=d["ruling"], reviewed_by=d["reviewed_by"])
        elif len(p) == 3 and p[2] == "contract":
            self._call("sign_contract", case_code=p[1], milestones=d["milestones"],
                       signed_by=d["signed_by"])
        elif len(p) == 3 and p[2] == "conditions":
            self._call("clear_condition", case_code=p[1], condition_id=d["condition_id"],
                       cleared_by=d["cleared_by"])
        elif len(p) == 5 and p[2] == "milestones":
            self._milestone_route(p, d)
        elif len(p) == 3 and p[2] == "terminate":
            self._call("terminate", case_code=p[1], reason=d["reason"],
                       terminated_by=d["terminated_by"],
                       milestone_failure=bool(d.get("milestone_failure", False)))
        elif len(p) == 3 and p[2] == "defrost":
            self._call("defrost", case_code=p[1], actor=d["actor"], memo=d.get("memo", ""))
        else:
            self._error(404, "not_found", f"未知路径: {self.path}")

    def _milestone_route(self, p: list[str], d: dict[str, Any]) -> None:
        case_code, seq = p[1], _int(p[3])
        action = p[4]
        if action == "evidence":
            self._call("submit_evidence", case_code=case_code, seq=seq,
                       evidence_ref=d["evidence_ref"], submitted_by=d["submitted_by"])
        elif action == "tech-review":
            self._call("tech_review", case_code=case_code, seq=seq,
                       approved=bool(d["approved"]), review_ref=d["review_ref"],
                       reviewer=d["reviewer"], note=d.get("note", ""))
        elif action == "finance-review":
            self._call("finance_review", case_code=case_code, seq=seq,
                       approved=bool(d["approved"]), review_ref=d["review_ref"],
                       reviewer=d["reviewer"], note=d.get("note", ""))
        elif action == "disbursements":
            self._call("disburse", case_code=case_code, seq=seq,
                       idempotency_key=d["idempotency_key"],
                       requested_by=d.get("requested_by", "treasury"),
                       reference=d.get("reference"))
        elif action == "recall":
            self._call("recall_disbursement", case_code=case_code, seq=seq,
                       reason=d["reason"], recalled_by=d["recalled_by"])
        else:
            self._error(404, "not_found", f"未知里程碑操作: {action}")


class _Handled(Exception):
    pass


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise FundError("validation", f"里程碑序号必须是整数: {value}") from exc


def _json_default(value: Any) -> Any:
    return str(value)


def create_server(host: str, port: int, db_path: str = ":memory:") -> ThreadingHTTPServer:
    service = build_service(db_path)
    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="机器人产业基金评审与拨款 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="industry_fund.db")
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.db)
    print(f"listening on http://{args.host}:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
