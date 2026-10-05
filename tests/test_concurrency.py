"""并发场景：多评委同时投票、重复付款请求并发。"""

import threading
import unittest

from industry_fund.errors import ConflictStateError

from tests._support import make_service, seed_reviewers


def _open_case_with_assignment(svc, n_reviewers=4, parties=None):
    parties = parties if parties is not None else [
        {"party": "无关关联方X", "relationship": "顾问"}]
    svc.create_case("c1", "C-1", "core_components", "甲", "甲公司", "1000",
                    actor="甲")
    svc.submit_application("c1", "路线", [{"name": "甲"}], {"x": 1}, parties,
                           actor="甲")
    svc.open_review_round("c1")
    svc.assign_reviewers("c1", min_reviewers=n_reviewers)
    return [a["reviewer_id"]
            for a in svc.case_view("c1")["rounds"][-1]["assignments"]]


class ConcurrentVoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store, self.path = make_service()
        seed_reviewers(self.svc)
        self.assigned = _open_case_with_assignment(self.svc)

    def test_parallel_scores_all_recorded(self) -> None:
        barrier = threading.Barrier(len(self.assigned))
        errors: list[Exception] = []

        def vote(rid: str, value: int) -> None:
            try:
                barrier.wait()
                self.svc.submit_score(
                    "c1", rid,
                    {"technology": value, "team": value, "market": value,
                     "compliance": value})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=vote, args=(rid, 70 + i))
            for i, rid in enumerate(self.assigned)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        scores = self.svc.case_view("c1")["rounds"][-1]["scores"]
        self.assertEqual(len(scores), len(self.assigned))

    def test_duplicate_concurrent_score_from_same_reviewer(self) -> None:
        rid = self.assigned[0]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def vote() -> None:
            try:
                barrier.wait()
                self.svc.submit_score(
                    "c1", rid,
                    {"technology": 80, "team": 80, "market": 80, "compliance": 80})
                with lock:
                    outcomes.append("ok")
            except ConflictStateError:
                with lock:
                    outcomes.append("rejected")
            except Exception as exc:  # noqa: BLE001
                with lock:
                    outcomes.append(f"error:{type(exc).__name__}")

        threads = [threading.Thread(target=vote) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["ok", "rejected"])
        self.assertEqual(
            len(self.svc.case_view("c1")["rounds"][-1]["scores"]), 1)


if __name__ == "__main__":
    unittest.main()
