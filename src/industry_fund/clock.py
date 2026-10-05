"""可替换的时钟，便于测试任意日期的资金视图。"""

from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    """统一的 UTC 时间源；测试中可冻结或拨快。"""

    def __init__(self, frozen: datetime | None = None) -> None:
        self._frozen = frozen

    def now(self) -> datetime:
        if self._frozen is not None:
            return self._frozen
        return datetime.now(timezone.utc)

    def freeze(self, value: datetime) -> None:
        self._frozen = value

    def iso(self) -> str:
        return self.now().isoformat()
