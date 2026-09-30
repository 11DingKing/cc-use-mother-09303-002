"""规范化序列化与内容摘要，保证任意版本可复算、可比对。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_dumps(payload: Any) -> str:
    """稳定的规范化 JSON：键排序、无空白、非 ASCII 原样保留。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(payload: Any) -> str:
    """计算规范化载荷的 SHA-256 摘要。"""
    return hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()
