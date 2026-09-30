"""测试夹具。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from skillmap.app import SkillMapApp


def welding_standard(prefix: str, *, version: str = "2024", extra_pipe_evidence: bool = False) -> dict:
    units = [
        {"code": f"{prefix}-PLATE", "title": "板对接焊", "level_label": "中级",
         "scope": "碳钢平板 1G/2G 8-20mm",
         "evidence": [
             {"code": "TH", "title": "理论", "evidence_kind": "THEORY", "requirement": "笔试≥80"},
             {"code": "PR", "title": "实操", "evidence_kind": "PRACTICAL", "requirement": "试件探伤合格"},
         ], "requires": []},
        {"code": f"{prefix}-PIPE", "title": "管对接焊", "level_label": "高级",
         "scope": "碳钢管道 5G/6G",
         "evidence": [
             {"code": "TH", "title": "理论", "evidence_kind": "THEORY", "requirement": "管道理论≥80"},
             {"code": "PR", "title": "实操", "evidence_kind": "PRACTICAL", "requirement": "6G 探伤合格"},
         ] + ([{"code": "OBS", "title": "现场观察", "evidence_kind": "OBSERVATION",
                "requirement": "现场观察 8 学时"}] if extra_pipe_evidence else []),
         "requires": [f"{prefix}-PLATE"]},
    ]
    return {"code": f"WELD-{prefix}", "title": f"焊接标准 {prefix}", "version_no": version,
            "publisher": f"{prefix} 工作组", "units": units}


class AppCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = SkillMapApp.open(":memory:")
        self.app.standards.create_jurisdiction("CN", "甲方管辖区")
        self.app.standards.create_jurisdiction("DE", "乙方管辖区")
        self.cn = self.app.standards.import_standard("CN", welding_standard("CN"))
        self.app.standards.mark_effective(self.cn)
        self.de = self.app.standards.import_standard("DE", welding_standard("DE", version="2023",
                                                                            extra_pipe_evidence=True))
        self.app.standards.mark_effective(self.de)

    def tearDown(self) -> None:
        self.app.close()

    def full_items(self) -> list[dict]:
        return [
            {"source_unit_code": "CN-PLATE", "target_unit_code": "DE-PLATE", "decision": "FULL",
             "rationale": "板仍等同", "gaps": []},
            {"source_unit_code": "CN-PIPE", "target_unit_code": "DE-PIPE", "decision": "PARTIAL",
             "rationale": "管位置范围有差异",
             "gaps": [{"gap_kind": "SCOPE_NARROWER", "source_evidence_code": "PR",
                       "target_evidence_code": "PR", "description": "6G 与固定管位置不等同"}]},
        ]

    def publish_first(self) -> tuple[int, int]:
        """创建并会签发布第一版，返回 (proposal_id, publish_id)。"""
        pid = self.app.proposals.create_proposal("互认 v1", self.cn, self.de, "专家甲", self.full_items())
        self.app.proposals.submit_for_review(pid, "专家甲")
        self.app.proposals.signoff(pid, "A", "甲方王", 1)
        r = self.app.proposals.signoff(pid, "B", "乙方 Müller", 1)
        return pid, r["publish_id"]
