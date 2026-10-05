"""仅追加事件存储：全局哈希链 + 乐观锁 + 命令级幂等。

所有状态变更都以事件形式追加到 SQLite；表级触发器禁止 UPDATE/DELETE，
事件之间以 sha256 全局串接，任何历史改写都会在 verify_chain 中暴露。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading
from typing import Callable

from .errors import ConcurrentModificationError, IdempotencyConflict, TamperDetectedError


def canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq: int
    aggregate_id: str
    aggregate_type: str
    version: int
    event_type: str
    payload: dict
    actor: str
    idem_key: str | None
    recorded_at: str
    prev_hash: str
    hash: str


class CommandContext:
    """命令回调在同一数据库事务内拥有的读写上下文。"""

    def __init__(self, store: "EventStore", conn: sqlite3.Connection, idem_key: str | None) -> None:
        self._store = store
        self.conn = conn
        self._idem_key = idem_key
        self.events: list[StoredEvent] = []
        self._last_seq, self._last_hash = store._tail_conn(conn)
        self._versions: dict[str, int] = {}

    def load(self, aggregate_id: str) -> list[StoredEvent]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE aggregate_id = ? ORDER BY seq", (aggregate_id,)
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def load_all(self) -> list[StoredEvent]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [_row_to_event(r) for r in rows]

    def now(self) -> str:
        return self._store.now_iso()

    def version_of(self, aggregate_id: str) -> int:
        current = self._versions.get(aggregate_id)
        if current is None:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM events WHERE aggregate_id = ?",
                (aggregate_id,),
            ).fetchone()
            current = int(row["v"])
            self._versions[aggregate_id] = current
        return current

    def append(
        self,
        aggregate_id: str,
        aggregate_type: str,
        event_type: str,
        payload: dict,
        actor: str,
    ) -> StoredEvent:
        """追加事件；expected_version 取事务内实时版本，天然防并发覆盖。"""
        current = self.version_of(aggregate_id)
        next_seq = self._last_seq + 1
        next_version = current + 1
        recorded_at = self._store._now_iso()
        body = canonical_json(
            {
                "seq": next_seq,
                "aggregate_id": aggregate_id,
                "aggregate_type": aggregate_type,
                "version": next_version,
                "event_type": event_type,
                "payload": payload,
                "actor": actor,
                "idem_key": self._idem_key,
                "recorded_at": recorded_at,
                "prev_hash": self._last_hash,
            }
        )
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        event = StoredEvent(
            seq=next_seq,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            version=next_version,
            event_type=event_type,
            payload=payload,
            actor=actor,
            idem_key=self._idem_key,
            recorded_at=recorded_at,
            prev_hash=self._last_hash,
            hash=digest,
        )
        self.conn.execute(
            """INSERT INTO events
               (seq, aggregate_id, aggregate_type, version, event_type, payload,
                actor, idem_key, recorded_at, prev_hash, hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.seq,
                event.aggregate_id,
                event.aggregate_type,
                event.version,
                event.event_type,
                canonical_json(event.payload),
                event.actor,
                event.idem_key,
                event.recorded_at,
                event.prev_hash,
                event.hash,
            ),
        )
        self._last_seq, self._last_hash = next_seq, digest
        self._versions[aggregate_id] = next_version
        self.events.append(event)
        return event

    def reserve_payment_ref(self, payment_ref: str, event_seq: int) -> None:
        """支付流水号唯一占用；重复提交在事务内直接失败。"""
        try:
            self.conn.execute(
                "INSERT INTO payment_refs (payment_ref, event_seq) VALUES (?,?)",
                (payment_ref, event_seq),
            )
        except sqlite3.IntegrityError as exc:
            raise ConcurrentModificationError(f"支付流水号 {payment_ref} 已存在，不能重复付款") from exc


class EventStore:
    def __init__(self, path: str, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                aggregate_id TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                actor TEXT NOT NULL,
                idem_key TEXT,
                recorded_at TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL,
                UNIQUE(aggregate_id, version)
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events 表仅追加，禁止更新');
            END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events 表仅追加，禁止删除');
            END;
            CREATE TABLE IF NOT EXISTS idempotency (
                idem_key TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                seqs TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payment_refs (
                payment_ref TEXT PRIMARY KEY,
                event_seq INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def _now_iso(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("时钟必须返回带时区的 datetime")
        return now.astimezone(timezone.utc).isoformat()

    def now_iso(self) -> str:
        return self._now_iso()

    @staticmethod
    def _tail_conn(conn: sqlite3.Connection) -> tuple[int, str]:
        row = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (0, "GENESIS") if row is None else (row["seq"], row["hash"])

    def run_command(
        self,
        idem_key: str | None,
        request_fingerprint: str,
        handler: Callable[[CommandContext], None],
    ) -> tuple[list[StoredEvent], bool]:
        """在一个事务内执行命令；idem_key 重复且请求体一致时重放历史结果。

        返回 (事件列表, 是否为重放)。重放不产生任何新写入、不触发任何副作用。
        """
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                if idem_key is not None:
                    row = conn.execute(
                        "SELECT fingerprint, seqs FROM idempotency WHERE idem_key = ?",
                        (idem_key,),
                    ).fetchone()
                    if row is not None:
                        if row["fingerprint"] != request_fingerprint:
                            raise IdempotencyConflict(
                                f"幂等键 {idem_key} 已用于不同的请求体"
                            )
                        seqs = json.loads(row["seqs"])
                        placeholders = ",".join("?" for _ in seqs)
                        rows = conn.execute(
                            f"SELECT * FROM events WHERE seq IN ({placeholders}) ORDER BY seq",
                            seqs,
                        ).fetchall()
                        conn.rollback()
                        return [_row_to_event(r) for r in rows], True

                ctx = CommandContext(self, conn, idem_key)
                handler(ctx)
                if not ctx.events:
                    raise RuntimeError("命令未产生任何事件")
                if idem_key is not None:
                    conn.execute(
                        "INSERT INTO idempotency (idem_key, fingerprint, aggregate_id, seqs, recorded_at) "
                        "VALUES (?,?,?,?,?)",
                        (
                            idem_key,
                            request_fingerprint,
                            ctx.events[0].aggregate_id,
                            json.dumps([e.seq for e in ctx.events]),
                            ctx.events[0].recorded_at,
                        ),
                    )
                conn.commit()
                return ctx.events, False
            except Exception:
                conn.rollback()
                raise

    def load_stream(self, aggregate_id: str) -> list[StoredEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE aggregate_id = ? ORDER BY seq", (aggregate_id,)
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def load_all(self) -> list[StoredEvent]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [_row_to_event(r) for r in rows]

    def verify_chain(self) -> None:
        """逐条重算哈希并校验全局串接；失败即说明历史被改写。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        prev = "GENESIS"
        expected_seq = 0
        for row in rows:
            expected_seq += 1
            if row["seq"] != expected_seq:
                raise TamperDetectedError(f"事件序号不连续: {row['seq']} != {expected_seq}")
            if row["prev_hash"] != prev:
                raise TamperDetectedError(f"事件 {row['seq']} 前向哈希断裂")
            body = canonical_json(
                {
                    "seq": row["seq"],
                    "aggregate_id": row["aggregate_id"],
                    "aggregate_type": row["aggregate_type"],
                    "version": row["version"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload"]),
                    "actor": row["actor"],
                    "idem_key": row["idem_key"],
                    "recorded_at": row["recorded_at"],
                    "prev_hash": row["prev_hash"],
                }
            )
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if digest != row["hash"]:
                raise TamperDetectedError(f"事件 {row['seq']} 内容哈希不匹配")
            prev = row["hash"]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_event(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        seq=row["seq"],
        aggregate_id=row["aggregate_id"],
        aggregate_type=row["aggregate_type"],
        version=row["version"],
        event_type=row["event_type"],
        payload=json.loads(row["payload"]),
        actor=row["actor"],
        idem_key=row["idem_key"],
        recorded_at=row["recorded_at"],
        prev_hash=row["prev_hash"],
        hash=row["hash"],
    )
