"""机器人产业基金评审与里程碑拨款的基础领域契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Money:
    """以整数分存储的金额，杜绝浮点误差；所有金额运算必须币种一致。"""

    cents: int
    currency: str = "CNY"

    def __post_init__(self) -> None:
        if not isinstance(self.cents, int):
            raise ValueError("金额必须为整数分")
        if self.cents < 0:
            raise ValueError("金额不能为负")
        if not self.currency.strip():
            raise ValueError("币种不能为空")

    def __add__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.cents + other.cents, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same_currency(other)
        return Money(self.cents - other.cents, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._same_currency(other)
        return self.cents < other.cents

    def __le__(self, other: "Money") -> bool:
        self._same_currency(other)
        return self.cents <= other.cents

    def _same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(f"币种不一致: {self.currency} != {other.currency}")

    @classmethod
    def yuan(cls, amount: float | int | str) -> "Money":
        """从人民币元构造，采用两位小数字符串解析避免二进制误差。"""
        sign = 1
        text = str(amount).strip()
        if text.startswith("-"):
            sign = -1
            text = text[1:]
        if "." in text:
            whole, frac = text.split(".", 1)
            frac = (frac + "00")[:2]
        else:
            whole, frac = text, "00"
        cents = sign * (int(whole) * 100 + int(frac))
        return cls(cents=cents)

    def to_dict(self) -> dict[str, object]:
        return {"cents": self.cents, "currency": self.currency}


def canonical_fingerprint(payload: object) -> str:
    """对任意可 JSON 化内容生成稳定 SHA-256 摘要。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_default)
    return sha256(text.encode("utf-8")).hexdigest()


def _default(value: object) -> object:
    if isinstance(value, Money):
        return value.to_dict()
    return asdict(value)


@dataclass(frozen=True, slots=True)
class InvestmentCase:
    """保存最小且可校验的业务对象。"""

    case_code: str
    applicant: str
    round_name: str
    state: str

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{key} 不能为空")
            if isinstance(value, int) and value < 1:
                raise ValueError(f"{key} 必须大于零")

    def evolve(self, **changes: object) -> "InvestmentCase":
        """返回新版本，避免就地改写历史对象。"""
        return replace(self, **changes)

    def fingerprint(self) -> str:
        """生成稳定摘要，供幂等和审计使用。"""
        return canonical_fingerprint(asdict(self))


def unique_by_identity(items: Iterable[InvestmentCase]) -> list[InvestmentCase]:
    """按业务标识去重，并拒绝同标识不同内容。"""
    found: dict[str, InvestmentCase] = {}
    for item in items:
        key = str(getattr(item, "case_code"))
        previous = found.get(key)
        if previous is not None and previous.fingerprint() != item.fingerprint():
            raise ValueError(f"业务标识 {key} 对应的内容发生冲突")
        found[key] = item
    return [found[key] for key in sorted(found)]
