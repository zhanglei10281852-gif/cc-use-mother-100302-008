"""复式台账只读投影。

从资金类事件派生平账分录（任意时刻 借方合计 = 贷方合计 = 资金来源总额）：

    可用 available -> 已承诺 committed -> 冻结 frozen -> 已付 paid
            ^                |                  |            |
            +----------------+------------------+------------+
                         失败/核减/终止/撤回返还

来源分摊全程可追溯：
- 合同签署时按来源 FIFO 占用 available，并按里程碑顺序把来源占用进一步
  预分配到每个里程碑（milestone_committed）；
- 冻结只搬该里程碑自己的来源占用；技术/财务失败或核减也只返还它的占用；
- 付款限定在该里程碑冻结时的来源占用；撤回按付款占用原路返还。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .domain import (
    CONTRACT_SIGNED,
    FUND_ESTABLISHED,
    MILESTONE_FROZEN,
    MILESTONE_UNFROZEN,
    MILESTONE_PAID,
    MILESTONE_CLAWED_BACK,
    ALLOCATION_RELEASED,
)

ACC_AVAILABLE = "available"
ACC_COMMITTED = "committed"
ACC_FROZEN = "frozen"
ACC_PAID = "paid"
ACC_SOURCE = "source"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    event_seq: int
    at: str
    fund_id: str
    account: str
    debit_cents: int
    credit_cents: int
    case_id: str | None
    milestone_id: str | None
    source_id: str | None
    memo: str


def _consume(amount: int, pools: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """按给定来源顺序与余量消费 amount，返回 (source_id, 用量)。"""
    remaining = amount
    splits: list[tuple[str, int]] = []
    for source_id, cap in pools:
        if remaining <= 0:
            break
        take = min(max(cap, 0), remaining)
        if take > 0:
            splits.append((source_id, take))
            remaining -= take
    if remaining != 0:
        raise RuntimeError(f"台账资金不足，缺口 {remaining}")
    return splits


def _decrement(scope: dict[str, int], splits: list[tuple[str, int]]) -> None:
    for sid, amt in splits:
        scope[sid] = scope.get(sid, 0) - amt


def project_entries(events: Iterable) -> list[LedgerEntry]:
    """从事件流（按 seq 顺序）派生平账分录。"""
    entries: list[LedgerEntry] = []
    fund_bal: dict[tuple[str, str, str], int] = {}
    case_bal: dict[tuple[str, str], dict[str, int]] = {}
    milestone_committed: dict[str, dict[str, int]] = {}
    milestone_frozen: dict[str, dict[str, int]] = {}
    milestone_paid: dict[str, dict[str, int]] = {}
    sources_order: dict[str, list[str]] = {}

    def add(e, account, debit, credit, fund_id, source_id,
            case_id=None, milestone_id=None, memo="") -> None:
        entries.append(
            LedgerEntry(
                event_seq=e.seq, at=e.recorded_at, fund_id=fund_id, account=account,
                debit_cents=debit, credit_cents=credit, case_id=case_id,
                milestone_id=milestone_id, source_id=source_id, memo=memo,
            )
        )

    def post(splits, fund_id, src_acc, dst_acc, e, memo,
             case_id=None, milestone_id=None) -> None:
        for sid, amt in splits:
            fund_bal[(fund_id, sid, src_acc)] = fund_bal.get((fund_id, sid, src_acc), 0) - amt
            fund_bal[(fund_id, sid, dst_acc)] = fund_bal.get((fund_id, sid, dst_acc), 0) + amt
            add(e, dst_acc, amt, 0, fund_id, sid, case_id, milestone_id, memo)
            add(e, src_acc, 0, amt, fund_id, sid, case_id, milestone_id, memo)
            if case_id is not None:
                cs = case_bal.setdefault((case_id, src_acc), {})
                cd = case_bal.setdefault((case_id, dst_acc), {})
                cs[sid] = cs.get(sid, 0) - amt
                cd[sid] = cd.get(sid, 0) + amt

    for e in events:
        p = e.payload
        if e.event_type == FUND_ESTABLISHED:
            order: list[str] = []
            for src in p["sources"]:
                sid = src["source_id"]
                order.append(sid)
                key = (p["fund_id"], sid, ACC_AVAILABLE)
                fund_bal[key] = fund_bal.get(key, 0) + src["amount_cents"]
                add(e, ACC_AVAILABLE, src["amount_cents"], 0, p["fund_id"], sid,
                    memo=f"{src['name']} 出资")
                add(e, ACC_SOURCE, 0, src["amount_cents"], p["fund_id"], sid,
                    memo=f"{src['name']} 出资")
            sources_order[p["fund_id"]] = order

        elif e.event_type == CONTRACT_SIGNED:
            fund_id, case_id = p["fund_id"], p["case_id"]
            order = sources_order[fund_id]
            pools = [(sid, fund_bal.get((fund_id, sid, ACC_AVAILABLE), 0)) for sid in order]
            splits = _consume(int(p["total_amount_cents"]), pools)
            post(splits, fund_id, ACC_AVAILABLE, ACC_COMMITTED, e, "合同承诺", case_id=case_id)
            # 按里程碑顺序把承诺占用预分配到各里程碑
            remaining = list(splits)
            for mid, amt_cents in p.get("milestone_amounts", []):
                pools_now = [(sid, dict(remaining).get(sid, 0)) for sid, _ in remaining]
                take = _consume(int(amt_cents), pools_now)
                used = dict(take)
                left = [(sid, cap - used.get(sid, 0)) for sid, cap in remaining]
                remaining = [(sid, cap) for sid, cap in left if cap > 0]
                milestone_committed[mid] = dict(take)

        elif e.event_type == "MilestoneFrozen":
            fund_id, case_id, mid = p["fund_id"], p["case_id"], p["milestone_id"]
            order = sources_order[fund_id]
            scope = milestone_committed.get(mid, {})
            splits = _consume(int(p["amount_cents"]), [(sid, scope.get(sid, 0)) for sid in order])
            _decrement(scope, splits)
            milestone_frozen[mid] = dict(splits)
            post(splits, fund_id, ACC_COMMITTED, ACC_FROZEN, e, "里程碑冻结",
                 case_id=case_id, milestone_id=mid)

        elif e.event_type == MILESTONE_UNFROZEN:
            fund_id, case_id, mid = p["fund_id"], p["case_id"], p["milestone_id"]
            splits = list(milestone_frozen.pop(mid, {}).items())
            post(splits, fund_id, ACC_FROZEN, ACC_COMMITTED, e,
                 p.get("reason", "冻结解除"), case_id=case_id, milestone_id=mid)
            milestone_committed[mid] = dict(splits)

        elif e.event_type == MILESTONE_PAID:
            fund_id, case_id, mid = p["fund_id"], p["case_id"], p["milestone_id"]
            splits = list(milestone_frozen.pop(mid, {}).items())
            milestone_paid[mid] = dict(splits)
            post(splits, fund_id, ACC_FROZEN, ACC_PAID, e, "里程碑付款",
                 case_id=case_id, milestone_id=mid)

        elif e.event_type == MILESTONE_CLAWED_BACK:
            fund_id, case_id, mid = p["fund_id"], p["case_id"], p["milestone_id"]
            order = sources_order[fund_id]
            scope = milestone_paid.setdefault(mid, {})
            splits = _consume(int(p["amount_cents"]), [(sid, scope.get(sid, 0)) for sid in order])
            _decrement(scope, splits)
            post(splits, fund_id, ACC_PAID, ACC_AVAILABLE, e, "拨款撤回",
                 case_id=case_id, milestone_id=mid)

        elif e.event_type == ALLOCATION_RELEASED:
            fund_id = p["fund_id"]
            order = sources_order[fund_id]
            from_acc = p.get("from_account", ACC_COMMITTED)
            mid, case_id, amt = p.get("milestone_id"), p.get("case_id"), int(p["amount_cents"])
            if mid is not None and from_acc == ACC_FROZEN and mid in milestone_frozen:
                scope = milestone_frozen.pop(mid, {})
                splits = _consume(amt, [(sid, scope.get(sid, 0)) for sid in order])
                milestone_committed.pop(mid, None)
            elif mid is not None and from_acc == ACC_COMMITTED and mid in milestone_committed:
                scope = milestone_committed[mid]
                splits = _consume(amt, [(sid, scope.get(sid, 0)) for sid in order])
                _decrement(scope, splits)
                if sum(scope.values()) <= 0:
                    milestone_committed.pop(mid, None)
            elif case_id is not None:
                scope = case_bal.get((case_id, from_acc), {})
                splits = _consume(amt, [(sid, scope.get(sid, 0)) for sid in order])
            else:
                pools = [(sid, fund_bal.get((fund_id, sid, from_acc), 0)) for sid in order]
                splits = _consume(amt, pools)
            post(splits, fund_id, from_acc, ACC_AVAILABLE, e,
                 p.get("reason", "额度释放"), case_id=case_id, milestone_id=mid)

    return entries


@dataclass(frozen=True, slots=True)
class FundSnapshot:
    fund_id: str
    as_of: str | None
    total_cents: int
    available_cents: int
    committed_cents: int
    frozen_cents: int
    paid_cents: int
    by_source: list[dict]
    by_case: list[dict]

    def check_conservation(self) -> None:
        """借方四科目之和恒等于来源总额。"""
        if (self.available_cents + self.committed_cents
                + self.frozen_cents + self.paid_cents) != self.total_cents:
            raise RuntimeError(
                f"资金不守恒: "
                f"{self.available_cents}+{self.committed_cents}+"
                f"{self.frozen_cents}+{self.paid_cents} != {self.total_cents}"
            )
        if sum(s["total_cents"] for s in self.by_source) != self.total_cents:
            raise RuntimeError("来源金额合计不等于基金总额")


def snapshot(entries: list[LedgerEntry], fund_id: str, as_of: str | None = None) -> FundSnapshot:
    """按日期（含当日，ISO 字符串比较）汇总；None 表示最新。"""
    by_source: dict[str, dict] = {}
    by_case: dict[str, dict] = {}
    total = 0

    def source_row(sid: str) -> dict:
        return by_source.setdefault(sid, {
            "source_id": sid, "total_cents": 0,
            "available_cents": 0, "committed_cents": 0,
            "frozen_cents": 0, "paid_cents": 0,
        })

    for e in entries:
        if e.fund_id != fund_id:
            continue
        if as_of is not None and e.at > as_of:
            continue
        if e.account == ACC_SOURCE:
            total += e.credit_cents
            source_row(e.source_id)["total_cents"] += e.credit_cents
            continue
        row = source_row(e.source_id)
        delta = e.debit_cents - e.credit_cents
        row[f"{e.account}_cents"] += delta
        if e.case_id:
            c = by_case.setdefault(e.case_id, {
                "case_id": e.case_id,
                "available_cents": 0, "committed_cents": 0,
                "frozen_cents": 0, "paid_cents": 0,
            })
            c[f"{e.account}_cents"] += delta

    snap = FundSnapshot(
        fund_id=fund_id,
        as_of=as_of,
        total_cents=total,
        available_cents=sum(s["available_cents"] for s in by_source.values()),
        committed_cents=sum(s["committed_cents"] for s in by_source.values()),
        frozen_cents=sum(s["frozen_cents"] for s in by_source.values()),
        paid_cents=sum(s["paid_cents"] for s in by_source.values()),
        by_source=sorted(by_source.values(), key=lambda x: x["source_id"] or ""),
        by_case=sorted(by_case.values(), key=lambda x: x["case_id"] or ""),
    )
    snap.check_conservation()
    return snap
