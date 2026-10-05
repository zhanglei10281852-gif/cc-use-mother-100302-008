"""金额处理：全程以整数"分"记账，输入用 Decimal 量化，杜绝浮点误差。"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

_CENTS = Decimal("100")
_QUANT = Decimal("0.01")

MoneyInput = Union[int, str, Decimal]


def to_cents(value: MoneyInput) -> int:
    """把元为单位的输入转为整数分；拒绝超过百分位的精度。"""
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    if isinstance(value, int):
        return value * 100
    try:
        dec = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - 统一转换为领域错误
        raise ValueError(f"无法解析金额: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError("金额必须是有限数")
    quantized = dec.quantize(_QUANT, rounding=ROUND_HALF_UP)
    return int(quantized * _CENTS)


def to_yuan(cents: int) -> str:
    """整数分转回元字符串。"""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"
