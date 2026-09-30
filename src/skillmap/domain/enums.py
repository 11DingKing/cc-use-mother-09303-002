"""领域枚举。"""
from __future__ import annotations

import enum


class StandardStatus(str, enum.Enum):
    DRAFT = "DRAFT"                # 导入：草稿，可编辑能力图谱
    EFFECTIVE = "EFFECTIVE"        # 生效版本，内容冻结
    SUPERSEDED = "SUPERSEDED"      # 被新版替代，只读存档


class ProposalStatus(str, enum.Enum):
    DRAFT = "DRAFT"                # 提案：逐项映射编辑中
    IN_REVIEW = "IN_REVIEW"        # 会审：等待双方会签
    PUBLISHED = "PUBLISHED"        # 发布：当前有效映射
    SUPERSEDED = "SUPERSEDED"      # 更新：被更新的发布版本替代
    WITHDRAWN = "WITHDRAWN"        # 撤回（含发布后的撤销互认）


class Decision(str, enum.Enum):
    FULL = "FULL"                  # 完全互认：范围与证据全部覆盖
    PARTIAL = "PARTIAL"            # 部分互认：存在明确差距
    NONE = "NONE"                  # 不予互认


class SignParty(str, enum.Enum):
    SOURCE = "A"                   # 标准来源方专家组
    TARGET = "B"                   # 合作方专家组


class GapKind(str, enum.Enum):
    SCOPE_NARROWER = "SCOPE_NARROWER"          # 操作范围更窄（如位置/材料/厚度）
    EVIDENCE_MISSING = "EVIDENCE_MISSING"      # 缺少对应考核证据
    EVIDENCE_TYPE = "EVIDENCE_TYPE"            # 证据类型不等同（如实操 vs 仅理论）
    PREREQUISITE = "PREREQUISITE"              # 前置能力未达成


class PublishKind(str, enum.Enum):
    MAPPING = "MAPPING"            # 正式发布的映射版本
    WITHDRAWAL = "WITHDRAWAL"      # 已发布映射的撤销版本


class RelationKind(str, enum.Enum):
    REQUIRES = "REQUIRES"          # 必修前置
    RECOMMENDS = "RECOMMENDS"      # 建议前置
