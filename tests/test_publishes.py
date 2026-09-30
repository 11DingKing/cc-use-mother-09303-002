"""发布版本、历史证书钉版、换版更新与任意两版比较/复算。"""
from __future__ import annotations

from tests._helpers import AppCase, welding_standard
from skillmap.domain.errors import DomainValidationError, WorkflowError


class PublishAndCertificateTest(AppCase):
    def test_publish_freezes_immutable_lines(self) -> None:
        _, pub_id = self.publish_first()
        pub = self.app.publishes.get_publish(pub_id)
        self.assertEqual(len(pub["lines"]), 2)
        pipe = next(l for l in pub["lines"] if l["decision"] == "PARTIAL")
        self.assertEqual(len(pipe["gaps"]), 1)
        self.assertIsNone(pub["superseded_at"])

    def test_certificate_is_pinned_to_publish_at_issue(self) -> None:
        _, pub1 = self.publish_first()
        cid = self.app.publishes.issue_certificate("C-1", "张伟", self.cn, "中级", pub1, "机构甲")
        view = self.app.publishes.get_certificate(cid)
        self.assertEqual(view["adopted_publish"]["id"], pub1)
        self.assertFalse(view["current_mapping_changed"])

    def test_certificate_rejects_withdrawal_publish(self) -> None:
        pid, pub1 = self.publish_first()
        w = self.app.proposals.withdraw(pid, "组", "终止")
        with self.assertRaises(WorkflowError):
            self.app.publishes.issue_certificate("C-2", "李", self.cn, "中级", w["publish_id"], "机构")

    def test_certificate_rejects_wrong_standard(self) -> None:
        _, pub1 = self.publish_first()
        with self.assertRaises(DomainValidationError):
            self.app.publishes.issue_certificate("C-3", "王", self.de, "中级", pub1, "机构")

    def test_recompute_matches_stored_hash(self) -> None:
        _, pub1 = self.publish_first()
        r = self.app.comparison.recompute_publish_hash(pub1)
        self.assertTrue(r["matches"])

    def test_recompute_detects_tampered_publish_line(self) -> None:
        _, pub1 = self.publish_first()
        self.app.conn.execute("UPDATE publish_lines SET rationale='改写历史依据' WHERE publish_id=?",
                              (pub1,))
        self.app.conn.commit()
        r = self.app.comparison.recompute_publish_hash(pub1)
        self.assertFalse(r["matches"])


class VersionUpdateTest(AppCase):
    def _new_de_version(self) -> int:
        draft = self.app.standards.new_version_draft(
            self.de, welding_standard("DE", version="2026", extra_pipe_evidence=False))
        self.app.standards.mark_effective(draft)
        return draft

    def test_update_proposal_seeds_and_links_chain(self) -> None:
        _, pub1 = self.publish_first()
        de2 = self._new_de_version()
        upd = self.app.proposals.propose_update(pub1, "专家甲", new_target_standard_id=de2)
        detail = self.app.proposals.get_proposal(upd["proposal_id"])
        self.assertEqual(detail["based_on_publish_id"], pub1)
        self.assertEqual(upd["seeded_lines"], 2)
        # 旧版 PIPE 差距引用的 DE 侧 PR 证据仍在 → 保留
        seeded_pipe = next(i for i in detail["items"] if i["source_unit_code"] == "CN-PIPE")
        self.assertEqual(len(seeded_pipe["gaps"]), 1)

    def test_update_requires_a_changed_standard(self) -> None:
        _, pub1 = self.publish_first()
        with self.assertRaises(DomainValidationError):
            self.app.proposals.propose_update(pub1, "专家甲")

    def test_second_publish_continues_chain_and_supersedes_first(self) -> None:
        pid1, pub1 = self.publish_first()
        de2 = self._new_de_version()
        upd = self.app.proposals.propose_update(pub1, "专家甲", new_target_standard_id=de2)
        p2 = upd["proposal_id"]
        # 专家重新判定：两单元均可完全互认
        items = [
            {"source_unit_code": "CN-PLATE", "target_unit_code": "DE-PLATE", "decision": "FULL",
             "rationale": "板仍等同", "gaps": []},
            {"source_unit_code": "CN-PIPE", "target_unit_code": "DE-PIPE", "decision": "FULL",
             "rationale": "新版证据齐备且范围等同", "gaps": []},
        ]
        self.app.proposals.revise(p2, items, "专家甲", "新版全互认")
        self.app.proposals.submit_for_review(p2, "专家甲")
        self.app.proposals.signoff(p2, "A", "王", 2)
        r = self.app.proposals.signoff(p2, "B", "Müller", 2)
        pub2 = r["publish_id"]

        first = self.app.publishes.get_publish(pub1)
        second = self.app.publishes.get_publish(pub2)
        self.assertIsNotNone(first["superseded_at"])
        self.assertEqual(second["prev_publish_id"], pub1)
        self.assertEqual(self.app.proposals.get_proposal(pid1)["status"], "SUPERSEDED")

        # 历史证书钉在 pub1，沿后继链检测到当前已变化
        cid = self.app.publishes.issue_certificate("C-9", "赵", self.cn, "中级", pub1, "机构")
        view = self.app.publishes.get_certificate(cid)
        self.assertTrue(view["current_mapping_changed"])
        self.assertEqual(view["latest_publish"]["id"], pub2)

    def test_compare_two_versions_reports_diff(self) -> None:
        _, pub1 = self.publish_first()
        de2 = self._new_de_version()
        upd = self.app.proposals.propose_update(pub1, "专家甲", new_target_standard_id=de2)
        p2 = upd["proposal_id"]
        items = [
            {"source_unit_code": "CN-PLATE", "target_unit_code": "DE-PLATE", "decision": "FULL",
             "rationale": "板仍等同", "gaps": []},
            {"source_unit_code": "CN-PIPE", "target_unit_code": "DE-PIPE", "decision": "FULL",
             "rationale": "新版全互认", "gaps": []},
        ]
        self.app.proposals.revise(p2, items, "专家甲", "v2")
        self.app.proposals.submit_for_review(p2, "专家甲")
        self.app.proposals.signoff(p2, "A", "王", 2)
        pub2 = self.app.proposals.signoff(p2, "B", "Müller", 2)["publish_id"]

        cmp = self.app.comparison.compare(pub1, pub2)
        self.assertTrue(cmp["comparable"])
        self.assertEqual(cmp["summary"]["changed"], 1)
        changed = cmp["changed"][0]
        self.assertEqual(changed["source_unit_code"], "CN-PIPE")
        self.assertTrue(changed["decision_changed"])
        self.assertEqual(cmp["summary"]["partial_before"], 1)
        self.assertEqual(cmp["summary"]["full_after"], 2)
        self.assertEqual(cmp["summary"]["hash_a"],
                         self.app.publishes.get_publish(pub1)["mapping_hash"])

    def test_compare_withdrawal_versions(self) -> None:
        pid, pub1 = self.publish_first()
        w = self.app.proposals.withdraw(pid, "组", "终止")["publish_id"]
        cmp = self.app.comparison.compare(pub1, w)
        self.assertFalse(cmp["comparable"])

    def test_evidence_missing_gap_dropped_on_target_revision(self) -> None:
        # 初版：DE 管道缺 OBS 证据 → EVIDENCE_MISSING
        items = [
            {"source_unit_code": "CN-PLATE", "target_unit_code": "DE-PLATE", "decision": "FULL",
             "rationale": "板等同", "gaps": []},
            {"source_unit_code": "CN-PIPE", "target_unit_code": "DE-PIPE", "decision": "PARTIAL",
             "rationale": "DE 缺现场观察",
             "gaps": [{"gap_kind": "EVIDENCE_MISSING", "source_evidence_code": "OBS",
                       "target_evidence_code": None, "description": "DE 无现场观察要求"}]},
        ]
        # 夹具中 CN 无 OBS、DE 有 OBS；构造反向：使用 CN 侧带 OBS 的标准
        cn2_draft = self.app.standards.new_version_draft(
            self.cn, welding_standard("CN", version="2026", extra_pipe_evidence=True))
        self.app.standards.mark_effective(cn2_draft)
        pid = self.app.proposals.create_proposal("obs", cn2_draft, self.de, "甲", items)
        self.app.proposals.submit_for_review(pid, "甲")
        self.app.proposals.signoff(pid, "A", "王", 1)
        pub1 = self.app.proposals.signoff(pid, "B", "Müller", 1)["publish_id"]

        # DE 换版去掉 OBS 证据后再换版加回？直接换版为无 OBS 版
        de_new_draft = self.app.standards.new_version_draft(
            self.de, welding_standard("DE", version="2026", extra_pipe_evidence=False))
        self.app.standards.mark_effective(de_new_draft)
        upd = self.app.proposals.propose_update(pub1, "甲", new_target_standard_id=de_new_draft)
        # EVIDENCE_MISSING 是对旧图谱的事实断言 → 必须丢弃重判
        self.assertEqual(upd["dropped_gaps"], 1)
