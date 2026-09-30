"""标准版本与能力图谱：冻结、换版、前置环、篡改复算。"""
from __future__ import annotations

from tests._helpers import AppCase, welding_standard
from skillmap.domain.errors import DomainValidationError, WorkflowError


class StandardTest(AppCase):
    def test_effective_standard_freezes_content_hash(self) -> None:
        detail = self.app.standards.get_standard(self.cn)
        self.assertEqual(len(detail["units"]), 2)
        self.assertTrue(detail["content_hash"])
        self.assertTrue(self.app.standards.verify_content_hash(self.cn))

    def test_prerequisite_must_exist(self) -> None:
        bad = welding_standard("XX")
        bad["units"][0]["requires"] = ["NO-SUCH-UNIT"]
        with self.assertRaises(DomainValidationError) as ctx:
            self.app.standards.import_standard("CN", bad)
        self.assertTrue(any("不存在" in e for e in ctx.exception.errors))

    def test_prerequisite_cycle_rejected(self) -> None:
        bad = welding_standard("CY")
        bad["units"][0]["requires"] = ["CY-PIPE"]
        with self.assertRaises(DomainValidationError):
            self.app.standards.import_standard("DE", bad)

    def test_self_prerequisite_rejected(self) -> None:
        bad = welding_standard("SF")
        bad["units"][0]["requires"] = ["SF-PLATE"]
        with self.assertRaises(DomainValidationError):
            self.app.standards.import_standard("DE", bad)

    def test_duplicate_unit_code_rejected(self) -> None:
        bad = welding_standard("DD")
        bad["units"][1]["code"] = "DD-PLATE"
        with self.assertRaises(DomainValidationError):
            self.app.standards.import_standard("DE", bad)

    def test_invalid_evidence_kind_rejected(self) -> None:
        bad = welding_standard("EK")
        bad["units"][0]["evidence"][0]["evidence_kind"] = "GUESS"
        with self.assertRaises(DomainValidationError):
            self.app.standards.import_standard("DE", bad)

    def test_new_version_supersedes_parent_when_effective(self) -> None:
        v2_payload = welding_standard("CN", version="2026")
        draft = self.app.standards.new_version_draft(self.cn, v2_payload)
        self.assertEqual(self.app.standards.get_standard(self.cn)["status"], "EFFECTIVE")
        self.app.standards.mark_effective(draft)
        self.assertEqual(self.app.standards.get_standard(self.cn)["status"], "SUPERSEDED")
        self.assertEqual(self.app.standards.get_standard(self.cn)["superseded_by_id"], draft)
        self.assertEqual(self.app.standards.get_standard(draft)["status"], "EFFECTIVE")

    def test_new_version_requires_same_code(self) -> None:
        changed = welding_standard("CN", version="2026")
        changed["code"] = "OTHER"
        with self.assertRaises(DomainValidationError):
            self.app.standards.new_version_draft(self.cn, changed)

    def test_cannot_version_a_draft(self) -> None:
        raw = self.app.standards.import_standard("DE", welding_standard("ZZ"))  # 未生效
        with self.assertRaises(WorkflowError):
            self.app.standards.new_version_draft(raw, welding_standard("ZZ", version="2"))

    def test_effective_standard_hash_is_immutable_record(self) -> None:
        before = self.app.standards.get_standard(self.cn)["content_hash"]
        # 直接篡改底层图谱行，复算必须发现不一致
        self.app.conn.execute("UPDATE units SET scope='被篡改的范围' WHERE code='CN-PLATE'")
        self.app.conn.commit()
        self.assertFalse(self.app.standards.verify_content_hash(self.cn))
        self.assertNotEqual(before, self.app.standards.get_standard(self.cn))
