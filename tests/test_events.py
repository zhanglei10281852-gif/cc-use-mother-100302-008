"""事件存储：哈希链、幂等、禁改触发器、乐观并发。"""

import sqlite3
import threading
import unittest

from industry_fund import EventStore
from industry_fund.events import canonical_json
from industry_fund.errors import (
    ConcurrentModificationError, IdempotencyConflict, TamperDetectedError,
)

from tests._support import new_db_path


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = new_db_path()
        self.store = EventStore(self.path)

    def tearDown(self) -> None:
        self.store.close()

    def _append(self, agg="a1", etype="SomethingHappened", payload=None, actor="x",
                idem=None):
        body = payload or {"v": 1}
        return self.store.run_command(
            idem, canonical_json(body) if idem else "",
            lambda ctx: ctx.append(agg, "Case", etype, body, actor))

    def test_events_chain_and_version(self) -> None:
        r1, _ = self._append()
        r2, _ = self._append(payload={"v": 2})
        self.assertEqual(r1[0].version, 1)
        self.assertEqual(r2[0].version, 2)
        self.assertEqual(r2[0].prev_hash, r1[0].hash)
        self.store.verify_chain()

    def test_idempotent_replay_returns_same_events(self) -> None:
        first, replayed1 = self._append(idem="key-1")
        self.assertFalse(replayed1)
        second, replayed2 = self._append(idem="key-1")
        self.assertTrue(replayed2)
        self.assertEqual([e.seq for e in first], [e.seq for e in second])
        self.assertEqual(len(self.store.load_all()), 1)

    def test_idempotency_conflicting_body_rejected(self) -> None:
        self._append(payload={"v": 1}, idem="key-2")
        with self.assertRaises(IdempotencyConflict):
            self._append(payload={"v": 999}, idem="key-2")

    def test_update_and_delete_are_blocked_by_triggers(self) -> None:
        self._append()
        conn = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE events SET payload = '{}' WHERE seq = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM events WHERE seq = 1")
        conn.close()

    def test_tampering_payload_breaks_chain(self) -> None:
        self._append()
        # 绕过触发器需要先临时关闭；直接改文件级不可行，改为模拟：删除触发器后篡改
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TRIGGER events_no_update")
        conn.execute("UPDATE events SET payload = ? WHERE seq = 1",
                     (canonical_json({"v": 666}),))
        conn.commit()
        conn.close()
        with self.assertRaises(TamperDetectedError):
            self.store.verify_chain()

    def test_concurrent_commands_serialize_within_process(self) -> None:
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                barrier.wait()
                self.store.run_command(
                    None, "",
                    lambda ctx: ctx.append("a2", "Case", "X", {"t": 1}, "w"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.load_stream("a2")), 2)

    def test_payment_ref_unique(self) -> None:
        def do(ctx):
            e = ctx.append("pay", "Milestone", "MilestonePaid", {"a": 1}, "f")
            ctx.reserve_payment_ref("PAY-X", e.seq)
        self.store.run_command(None, "", do)

        def do_duplicate(ctx):
            e2 = ctx.append("pay2", "Milestone", "MilestonePaid", {"a": 1}, "f")
            ctx.reserve_payment_ref("PAY-X", e2.seq)
        with self.assertRaises(ConcurrentModificationError):
            self.store.run_command(None, "", do_duplicate)
        # 冲突命令整体回滚：不留下事件
        self.assertEqual(self.store.load_stream("pay2"), [])


if __name__ == "__main__":
    unittest.main()
