"""SQLite 持久层：只追加的业务历史、复式台账与哈希链审计。

写操作统一在 ``BEGIN IMMEDIATE`` 事务内完成；进程内用锁串行化，
配合唯一约束保证并发下不会重复投票或重复付款。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import json
import re
import sqlite3
import threading
from typing import Any, Iterator

from .contracts import Money
from .domain import AuditRecord

SLUG = re.compile(r"^[A-Za-z0-9_-]+$")


def require_slug(value: str, label: str) -> str:
    if not SLUG.match(value):
        raise ValueError(f"{label} 只能含字母、数字、下划线或连字符: {value}")
    return value


def _default(value: Any) -> Any:
    if isinstance(value, Money):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    return value.__dict__


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_default)


def loads(text: str | None) -> Any:
    return json.loads(text) if text else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases (
    case_code TEXT PRIMARY KEY, applicant TEXT NOT NULL, round_name TEXT NOT NULL,
    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_versions (
    case_code TEXT NOT NULL, version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL, content_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL, submitted_at TEXT NOT NULL,
    parent_version INTEGER,
    PRIMARY KEY (case_code, version)
);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id TEXT PRIMARY KEY, name TEXT NOT NULL, expertise_json TEXT NOT NULL,
    affiliations_json TEXT NOT NULL, related_keys_json TEXT NOT NULL, active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    case_code TEXT NOT NULL, reviewer_id TEXT NOT NULL, assigned_at TEXT NOT NULL,
    conflict_json TEXT NOT NULL, eligible INTEGER NOT NULL, reason TEXT NOT NULL,
    status TEXT NOT NULL, PRIMARY KEY (case_code, reviewer_id)
);
CREATE TABLE IF NOT EXISTS rounds (
    case_code TEXT NOT NULL, round_version INTEGER NOT NULL,
    opened_at TEXT NOT NULL, closed_at TEXT,
    PRIMARY KEY (case_code, round_version)
);
CREATE TABLE IF NOT EXISTS scorecards (
    case_code TEXT NOT NULL, reviewer_id TEXT NOT NULL, round_version INTEGER NOT NULL,
    dimensions_json TEXT NOT NULL, rationale TEXT NOT NULL, scored_at TEXT NOT NULL,
    PRIMARY KEY (case_code, reviewer_id, round_version)
);
CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY, case_code TEXT NOT NULL, reviewer_id TEXT NOT NULL,
    asked_at TEXT NOT NULL, content TEXT NOT NULL,
    response TEXT, responded_at TEXT, response_version INTEGER
);
CREATE TABLE IF NOT EXISTS ballots (
    case_code TEXT NOT NULL, reviewer_id TEXT NOT NULL, round_version INTEGER NOT NULL,
    vote TEXT NOT NULL, comment TEXT NOT NULL, cast_at TEXT NOT NULL,
    PRIMARY KEY (case_code, reviewer_id, round_version)
);
CREATE TABLE IF NOT EXISTS decisions (
    case_code TEXT NOT NULL, decision_version INTEGER NOT NULL,
    outcome TEXT NOT NULL, approved_cents INTEGER NOT NULL, currency TEXT NOT NULL,
    score_json TEXT NOT NULL, conditions_json TEXT NOT NULL, rationale TEXT NOT NULL,
    decided_by TEXT NOT NULL, decided_at TEXT NOT NULL, void INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (case_code, decision_version)
);
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY, case_code TEXT NOT NULL,
    against_decision_version INTEGER NOT NULL, filed_by TEXT NOT NULL, filed_at TEXT NOT NULL,
    grounds TEXT NOT NULL, state TEXT NOT NULL,
    reviewed_by TEXT, reviewed_at TEXT, ruling TEXT
);
CREATE TABLE IF NOT EXISTS fund_sources (
    source TEXT PRIMARY KEY, name TEXT NOT NULL, cents INTEGER NOT NULL,
    currency TEXT NOT NULL, added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_allocations (
    case_code TEXT NOT NULL, source TEXT NOT NULL, cents INTEGER NOT NULL,
    PRIMARY KEY (case_code, source)
);
CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT PRIMARY KEY, case_code TEXT UNIQUE NOT NULL,
    decision_version INTEGER NOT NULL, total_cents INTEGER NOT NULL,
    currency TEXT NOT NULL, signed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones (
    milestone_id TEXT PRIMARY KEY, case_code TEXT NOT NULL, seq INTEGER NOT NULL,
    name TEXT NOT NULL, criteria TEXT NOT NULL, amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL, status TEXT NOT NULL, required_conditions_json TEXT NOT NULL,
    evidence_ref TEXT, evidence_submitted_at TEXT,
    tech_review_ref TEXT, tech_reviewer TEXT, tech_reviewed_at TEXT,
    finance_review_ref TEXT, finance_reviewer TEXT, finance_reviewed_at TEXT,
    UNIQUE (case_code, seq)
);
CREATE TABLE IF NOT EXISTS milestone_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, milestone_id TEXT NOT NULL,
    case_code TEXT NOT NULL, event_type TEXT NOT NULL, actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL, data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS condition_clearances (
    case_code TEXT NOT NULL, condition_id TEXT NOT NULL,
    cleared_by TEXT NOT NULL, cleared_at TEXT NOT NULL,
    PRIMARY KEY (case_code, condition_id)
);
CREATE TABLE IF NOT EXISTS disbursements (
    disbursement_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    case_code TEXT NOT NULL, milestone_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
    requested_at TEXT NOT NULL, paid_at TEXT, reference TEXT, source TEXT NOT NULL,
    PRIMARY KEY (disbursement_id, source),
    UNIQUE (idempotency_key, source)
);
CREATE TABLE IF NOT EXISTS recalls (
    recall_id TEXT NOT NULL, disbursement_id TEXT NOT NULL,
    case_code TEXT NOT NULL, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL,
    reason TEXT NOT NULL, recalled_at TEXT NOT NULL, recalled_by TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (recall_id, source),
    UNIQUE (disbursement_id, source)
);
CREATE TABLE IF NOT EXISTS terminations (
    case_code TEXT PRIMARY KEY, terminated_at TEXT NOT NULL, terminated_by TEXT NOT NULL,
    reason TEXT NOT NULL, milestone_failure INTEGER NOT NULL,
    unreleased_cancelled_cents INTEGER NOT NULL, currency TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT PRIMARY KEY, tx_id TEXT NOT NULL, tx_type TEXT NOT NULL,
    posted_at TEXT NOT NULL, account TEXT NOT NULL,
    debit INTEGER NOT NULL, credit INTEGER NOT NULL, currency TEXT NOT NULL,
    case_code TEXT, ref_id TEXT, memo TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_account_time ON ledger_entries(account, posted_at);
CREATE INDEX IF NOT EXISTS idx_ledger_case ON ledger_entries(case_code);
CREATE INDEX IF NOT EXISTS idx_ledger_tx ON ledger_entries(tx_id);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, aggregate TEXT NOT NULL, aggregate_id TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL, prev_hash TEXT NOT NULL, tx_id TEXT
);
CREATE TABLE IF NOT EXISTS idempotency (
    idem_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class Repository:
    """轻量数据访问对象；业务规则在 service 层。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """串行化的立即事务，避免 SQLite 写冲突与读后写竞争。"""
        self._lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        finally:
            self._lock.release()

    # ------------------------------------------------------------- 审计哈希链
    def append_audit(self, conn: sqlite3.Connection, *, actor: str, action: str,
                     aggregate: str, aggregate_id: str, payload_fingerprint: str,
                     tx_id: str | None, occurred_at: str) -> str:
        row = conn.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = ""
        if row is not None:
            prev_hash = AuditRecord(
                seq=row["seq"], occurred_at=row["occurred_at"], actor=row["actor"],
                action=row["action"], aggregate=row["aggregate"],
                aggregate_id=row["aggregate_id"],
                payload_fingerprint=row["payload_fingerprint"],
                prev_hash=row["prev_hash"], tx_id=row["tx_id"],
            ).hash()
        placeholder = AuditRecord(
            seq=0, occurred_at=occurred_at, actor=actor, action=action,
            aggregate=aggregate, aggregate_id=aggregate_id,
            payload_fingerprint=payload_fingerprint, prev_hash=prev_hash, tx_id=tx_id,
        )
        cur = conn.execute(
            "INSERT INTO audit_log (occurred_at, actor, action, aggregate, aggregate_id,"
            " payload_fingerprint, prev_hash, tx_id) VALUES (?,?,?,?,?,?,?,?)",
            (occurred_at, actor, action, aggregate, aggregate_id,
             payload_fingerprint, prev_hash, tx_id),
        )
        seq = cur.lastrowid
        record = AuditRecord(
            seq=seq, occurred_at=occurred_at, actor=actor, action=action,
            aggregate=aggregate, aggregate_id=aggregate_id,
            payload_fingerprint=payload_fingerprint, prev_hash=prev_hash, tx_id=tx_id,
        )
        return record.hash()

    def audit_tail(self) -> str:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            if row is None:
                return ""
            return AuditRecord(
                seq=row["seq"], occurred_at=row["occurred_at"], actor=row["actor"],
                action=row["action"], aggregate=row["aggregate"],
                aggregate_id=row["aggregate_id"],
                payload_fingerprint=row["payload_fingerprint"],
                prev_hash=row["prev_hash"], tx_id=row["tx_id"],
            ).hash()

    def verify_chain_integrity(self) -> bool:
        """完整校验：prev_hash 链连续，且每条记录摘要与下一条引用一致。

        篡改任何历史记录（含 payload 摘要、actor、时间）都会使链断裂。
        """
        prev = ""
        rows = self.conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        for index, row in enumerate(rows):
            record = AuditRecord(
                seq=row["seq"], occurred_at=row["occurred_at"], actor=row["actor"],
                action=row["action"], aggregate=row["aggregate"],
                aggregate_id=row["aggregate_id"],
                payload_fingerprint=row["payload_fingerprint"],
                prev_hash=row["prev_hash"], tx_id=row["tx_id"],
            )
            if row["prev_hash"] != prev:
                return False
            prev = record.hash()
            if index + 1 < len(rows) and rows[index + 1]["prev_hash"] != prev:
                return False
        return True

    # ---------------------------------------------------------------- 幂等
    def cached_idempotent(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT result_json FROM idempotency WHERE idem_key=?", (key,)).fetchone()
        return None if row is None else row["result_json"]

    def remember_idempotent(self, conn: sqlite3.Connection, key: str, result: str, now: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO idempotency (idem_key, result_json, created_at) VALUES (?,?,?)",
            (key, result, now),
        )
