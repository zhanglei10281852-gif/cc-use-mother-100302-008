"""复式台账：每个事务借贷相等，金额守恒可逐日核对。

账户命名：
- ``capital:<source>``            贷方，来源出资
- ``available:<source>``          借方，来源可用现金
- ``committed:<case>:<source>``   借方，已承诺未付
- ``frozen:<case>:<source>``      借方，冻结（条件争议/撤回待处理/里程碑失败）
- ``paid:<case>:<source>``        借方，已支付（费用）

资金始终不离开来源维度，因此可同时给出全局与按来源的资金视图。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections import defaultdict
from typing import Any

from .contracts import Money, canonical_fingerprint
from .errors import ConservationError


def new_tx_id() -> str:
    return uuid.uuid4().hex


def post(conn: sqlite3.Connection, *, tx_id: str, tx_type: str, posted_at: str,
         legs: list[tuple[str, int, int]], currency: str,
         case_code: str | None, ref_id: str | None, memo: str) -> None:
    """提交一组平衡分录；不平衡直接拒绝，整笔事务回滚。"""
    debit = sum(leg[1] for leg in legs)
    credit = sum(leg[2] for leg in legs)
    if debit != credit or debit <= 0:
        raise ConservationError(f"台账分录不平衡: 借{debit} 贷{credit} ({tx_type})")
    for account, d, c in legs:
        if d < 0 or c < 0:
            raise ConservationError("台账金额不能为负")
        entry_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO ledger_entries (entry_id, tx_id, tx_type, posted_at, account,"
            " debit, credit, currency, case_code, ref_id, memo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, tx_id, tx_type, posted_at, account, d, c, currency,
             case_code, ref_id, memo),
        )


def account_balances(conn: sqlite3.Connection, as_of: str | None = None) -> dict[str, int]:
    """返回各账户截至某日（含）的借方净额。"""
    query = "SELECT account, SUM(debit) - SUM(credit) AS bal FROM ledger_entries"
    params: tuple[Any, ...] = ()
    if as_of is not None:
        query += " WHERE posted_at <= ?"
        params = (as_of,)
    query += " GROUP BY account"
    return {row["account"]: row["bal"] or 0 for row in conn.execute(query, params)}


def case_balance_by_source(conn: sqlite3.Connection, prefix: str, case_code: str,
                           as_of: str | None = None) -> dict[str, int]:
    """汇总 ``<prefix>:<case>:<source>`` 形态账户，按来源返回余额。

    在 Python 中做精确前缀匹配，避免 case_code 中的下划线被当作 SQL LIKE 通配符。
    """
    query = ("SELECT account, SUM(debit)-SUM(credit) AS bal FROM ledger_entries "
             "WHERE case_code=?")
    params: list[Any] = [case_code]
    if as_of is not None:
        query += " AND posted_at <= ?"
        params.append(as_of)
    query += " GROUP BY account"
    account_prefix = f"{prefix}:{case_code}:"
    out: dict[str, int] = defaultdict(int)
    for row in conn.execute(query, params):
        if not row["account"].startswith(account_prefix):
            continue
        source = row["account"][len(account_prefix):]
        out[source] += row["bal"] or 0
    return dict(out)


def available_by_source(conn: sqlite3.Connection, as_of: str | None = None) -> dict[str, int]:
    balances = account_balances(conn, as_of)
    return {k.split(":", 1)[1]: v for k, v in balances.items()
            if k.startswith("available:") and v}


def pick_sources(remaining: dict[str, int], cents: int) -> list[tuple[str, int]]:
    """按来源键排序做 FIFO 扣减，保证支付不超出来源剩余承诺额度。"""
    alloc: list[tuple[str, int]] = []
    left = cents
    for source in sorted(remaining):
        amount = remaining[source]
        if amount <= 0:
            continue
        take = min(amount, left)
        if take:
            alloc.append((source, take))
            left -= take
        if left == 0:
            break
    if left:
        raise ConservationError("承诺来源剩余额度不足，金额守恒被打破")
    return alloc


def snapshot(conn: sqlite3.Connection, as_of: str, currency: str = "CNY") -> dict[str, Any]:
    """聚合任意日期的承诺/已付/冻结/可用及来源构成。"""
    balances = account_balances(conn, as_of)
    total = committed = paid = frozen = available = 0
    by_source: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "committed": 0, "paid": 0, "frozen": 0, "available": 0})
    for account, bal in balances.items():
        if bal == 0:
            continue
        parts = account.split(":")
        kind = parts[0]
        if kind == "capital":
            # 资本为贷方余额，取借方净额的相反数。
            source = parts[1]
            total += -bal
            by_source[source]["total"] += -bal
        elif kind == "available":
            source = parts[1]
            available += bal
            by_source[source]["available"] += bal
        elif kind in ("committed", "frozen", "paid"):
            _, case_code, source = parts
            committed += bal if kind == "committed" else 0
            frozen += bal if kind == "frozen" else 0
            paid += bal if kind == "paid" else 0
            by_source[source][kind] += bal
    if total != available + committed + frozen + paid:
        raise ConservationError(
            f"资金不守恒: 来源{total} != 可用{available}+承诺{committed}+冻结{frozen}+已付{paid}")
    return {
        "as_of": as_of,
        "currency": currency,
        "total_fund": Money(total, currency),
        "committed": Money(committed, currency),
        "paid": Money(paid, currency),
        "frozen": Money(frozen, currency),
        "available": Money(available, currency),
        "by_source": dict(sorted(by_source.items())),
    }


def verify_conservation(conn: sqlite3.Connection) -> None:
    """全量核对：每条事务借贷相等，且全局来源 = 四类占用之和。"""
    row = conn.execute(
        "SELECT tx_id, SUM(debit) d, SUM(credit) c FROM ledger_entries GROUP BY tx_id"
    ).fetchall()
    for item in row:
        if item["d"] != item["c"]:
            raise ConservationError(f"事务 {item['tx_id']} 借贷不等")
    balances = account_balances(conn)
    capital = sum(-v for k, v in balances.items() if k.startswith("capital:"))
    available = sum(v for k, v in balances.items() if k.startswith("available:"))
    other = sum(v for k, v in balances.items()
                if k.startswith(("committed:", "frozen:", "paid:")))
    if capital != available + other:
        raise ConservationError("全局资金不守恒")
    # 资本账户为贷方余额（净额为负是正常的）；其余账户不允许为负。
    for account, bal in balances.items():
        if bal < 0 and not account.startswith("capital:"):
            raise ConservationError(f"账户 {account} 余额为负")


def ledger_fingerprint(conn: sqlite3.Connection) -> str:
    """台账整体摘要，随审计事件落链，便于外部比对。"""
    rows = conn.execute(
        "SELECT tx_id, account, debit, credit FROM ledger_entries ORDER BY entry_id"
    ).fetchall()
    return canonical_fingerprint([(r["tx_id"], r["account"], r["debit"], r["credit"]) for r in rows])
