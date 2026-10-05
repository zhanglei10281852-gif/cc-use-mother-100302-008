"""领域错误：服务层以带错误码的异常拒绝非法操作。"""

from __future__ import annotations


class FundError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConflictError(FundError):
    """并发或唯一约束冲突（重复投票、重复付款等）。"""

    def __init__(self, message: str) -> None:
        super().__init__("conflict", message)


class ValidationError(FundError):
    def __init__(self, message: str) -> None:
        super().__init__("validation", message)


class StateError(FundError):
    def __init__(self, message: str) -> None:
        super().__init__("state", message)


class NotFoundError(FundError):
    def __init__(self, message: str) -> None:
        super().__init__("not_found", message)


class ConservationError(FundError):
    def __init__(self, message: str) -> None:
        super().__init__("conservation", message)
