"""领域错误。"""
from __future__ import annotations


class SkillMapError(Exception):
    """所有领域错误的基类。"""


class NotFoundError(SkillMapError):
    """引用的聚合不存在。"""


class DomainValidationError(SkillMapError):
    """违反领域不变量，附带全部错误项。"""

    def __init__(self, errors: str | list[str]):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("；".join(self.errors))


class WorkflowError(SkillMapError):
    """提案状态机不允许该操作。"""


class SigningConditionError(SkillMapError):
    """尚未达到双方会签发布条件。"""


class ConcurrentSignoffError(SkillMapError):
    """并发会签导致的乐观锁冲突，调用方应重读后重试。"""
