"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则违反的基类。"""


class ValidationError(DomainError):
    """输入不满足契约。"""


class NotFoundError(DomainError):
    """聚合或资源不存在。"""


class ConflictStateError(DomainError):
    """聚合当前状态不允许该操作（含里程碑失败后的拨款阻断）。"""


class ConflictOfInterestError(DomainError):
    """存在回避关系，评委不得参与该项目。"""


class ConcurrentModificationError(DomainError):
    """乐观锁：事件流已被其他请求推进，调用方需重读后重试。"""


class IdempotencyConflict(DomainError):
    """同一幂等键被用于不同的请求体。"""


class TamperDetectedError(DomainError):
    """审计链校验失败，历史可能被改写。"""
