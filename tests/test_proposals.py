"""映射提案：逐项校验、完整性、修订版本、会签条件、撤回。"""
from __future__ import annotations

import threading

from tests._helpers import AppCase, welding_standard
from skillmap.domain.errors import (
    ConcurrentSignoffError,
    DomainValidationError,
    SigningConditionError,
    WorkflowError,
)


class ProposalValidationTest(AppCase):
    def test_cross_jurisdiction_required(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.app.proposals.create_proposal("同区", self.cn, self.cn, "x", self.full_items())

    def test_effective_standard_only(self) -> None:
        draft_std = self.app.standards.import_standard("DE", welding_standard("ZZ"))
        with self.assertRaises(WorkflowError):
            self.app.proposals.create_proposal("引用草稿", self.cn, draft_std, "x", [])

    def test_unknown_source_unit_rejected(self) -> None:
        items = self.full_items()
        items[0]["source_unit_code"] = "GHOST"
        with self.assertRaises(DomainValidationError) as ctx:
            self.app.proposals.create_proposal("p", self.cn, self.de, "x", items)
        self.assertTrue(any("来源能力单元不存在" in e for e in ctx.exception.errors))

    def test_rationale_required_per_item(self) -> None:
        items = self.full_items()
        items[0]["rationale"] = "  "
        with self.assertRaises(DomainValidationError):
            self.app.proposals.create_proposal("p", self.cn, self.de, "x", items)

    def test_partial_requires_gaps(self) -> None:
        items = self.full_items()
        items[1]["gaps"] = []
        with self.assertRaises(DomainValidationError):
            self.app.proposals.create_proposal("p", self.cn, self.de, "x", items)

    def test_full_must_not_have_gaps(self) -> None:
        items = self.full_items()
        items[0]["gaps"] = [{"gap_kind": "SCOPE_NARROWER", "description": "矛盾项"}]
        with self.assertRaises(DomainValidationError):
            self.app.proposals.create_proposal("p", self.cn, self.de, "x", items)

    def test_full_requires_target_unit(self) -> None:
        items = self.full_items()
        items[0]["target_unit_code"] = None
        with self.assertRaises(DomainValidationError):
            self.app.proposals.create_proposal("p", self.cn, self.de, "x", items)

    def test_unknown_evidence_in_gap_rejected(self) -> None:
        items = self.full_items()
        items[1]["gaps"][0]["source_evidence_code"] = "NO-EV"
        with self.assertRaises(DomainValidationError):
            self.app.proposals.create_proposal("p", self.cn, self.de, "x", items)

    def test_submit_requires_exhaustive_coverage(self) -> None:
        # 起草时允许只映射部分单元；提交会审必须逐项覆盖全部来源单元
        pid = self.app.proposals.create_proposal(
            "p", self.cn, self.de, "x", [self.full_items()[0]])
        with self.assertRaises(DomainValidationError) as ctx:
            self.app.proposals.submit_for_review(pid, "x")
        self.assertTrue(any("尚未逐项映射" in e for e in ctx.exception.errors))

    def test_duplicate_source_unit_in_items_rejected(self) -> None:
        items = self.full_items()
        items.append(dict(items[0]))
        with self.assertRaises(DomainValidationError):
            self.app.proposals.create_proposal("p", self.cn, self.de, "x", items)


class RevisionWorkflowTest(AppCase):
    def test_revision_creates_new_version_and_history(self) -> None:
        pid = self.app.proposals.create_proposal("p", self.cn, self.de, "甲", self.full_items())
        self.assertEqual(self.app.proposals.get_proposal(pid)["revision_no"], 1)
        items = self.full_items()
        items[1]["rationale"] = "补充依据：经双方试件比对"
        rev = self.app.proposals.revise(pid, items, "甲", "补充依据")
        self.assertEqual(rev, 2)
        detail = self.app.proposals.get_proposal(pid)
        self.assertEqual(len(detail["revisions"]), 2)
        pipe = next(i for i in detail["items"] if i["source_unit_code"] == "CN-PIPE")
        self.assertEqual(pipe["rationale"], "补充依据：经双方试件比对")

    def test_request_changes_clears_signoffs_and_requires_new_revision_cycle(self) -> None:
        pid = self.app.proposals.create_proposal("p", self.cn, self.de, "甲", self.full_items())
        self.app.proposals.submit_for_review(pid, "甲")
        self.app.proposals.signoff(pid, "A", "王", 1)
        self.app.proposals.request_changes(pid, "Müller", "管道依据不足")
        detail = self.app.proposals.get_proposal(pid)
        self.assertEqual(detail["status"], "DRAFT")
        self.assertEqual(detail["signoffs"], [])

    def test_cannot_signoff_in_draft(self) -> None:
        pid = self.app.proposals.create_proposal("p", self.cn, self.de, "甲", self.full_items())
        with self.assertRaises(WorkflowError):
            self.app.proposals.signoff(pid, "A", "王", 1)

    def test_withdraw_draft(self) -> None:
        pid = self.app.proposals.create_proposal("p", self.cn, self.de, "甲", self.full_items())
        self.app.proposals.withdraw(pid, "甲", "放弃")
        self.assertEqual(self.app.proposals.get_proposal(pid)["status"], "WITHDRAWN")

    def test_published_then_withdraw_creates_withdrawal_version(self) -> None:
        pid, pub = self.publish_first()
        result = self.app.proposals.withdraw(pid, "工作组", "终止")
        self.assertEqual(result["kind"], "WITHDRAWAL")
        self.assertEqual(self.app.proposals.get_proposal(pid)["status"], "WITHDRAWN")
        old = self.app.publishes.get_publish(pub)
        self.assertIsNotNone(old["superseded_at"])
        # 历史发布行仍然完整可读
        self.assertEqual(len(old["lines"]), 2)


class SignoffConcurrencyTest(AppCase):
    def test_duplicate_signoff_rejected(self) -> None:
        pid = self.app.proposals.create_proposal("p", self.cn, self.de, "甲", self.full_items())
        self.app.proposals.submit_for_review(pid, "甲")
        self.app.proposals.signoff(pid, "A", "王", 1)
        with self.assertRaises(ConcurrentSignoffError):
            self.app.proposals.signoff(pid, "A", "王重复", 1)

    def test_stale_revision_rejected(self) -> None:
        pid = self.app.proposals.create_proposal("p", self.cn, self.de, "甲", self.full_items())
        self.app.proposals.submit_for_review(pid, "甲")
        # 会审中被退回、修订后重新提交：revision_no=2，旧客户端仍持 1
        self.app.proposals.request_changes(pid, "乙", "改")
        self.app.proposals.revise(pid, self.full_items(), "甲", "v2")
        self.app.proposals.submit_for_review(pid, "甲")
        with self.assertRaises(ConcurrentSignoffError):
            self.app.proposals.signoff(pid, "A", "王", expected_revision=1)
        ok = self.app.proposals.signoff(pid, "A", "王", expected_revision=2)
        self.assertFalse(ok["all_signed"])

    def test_concurrent_signoff_only_one_publishes(self) -> None:
        pid = self.app.proposals.create_proposal("p", self.cn, self.de, "甲", self.full_items())
        self.app.proposals.submit_for_review(pid, "甲")
        outcomes: list[dict] = []
        barrier = threading.Barrier(2)

        def sign(party: str, signer: str) -> None:
            barrier.wait()
            try:
                outcomes.append(self.app.proposals.signoff(pid, party, signer, 1))
            except ConcurrentSignoffError:
                outcomes.append({"conflict": True})

        threads = [threading.Thread(target=sign, args=("A", "王")),
                   threading.Thread(target=sign, args=("B", "Müller"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        publishes = [o.get("publish_id") for o in outcomes if o.get("publish_id")]
        self.assertEqual(len(publishes), 1)
        self.assertEqual(self.app.proposals.get_proposal(pid)["status"], "PUBLISHED")

    def test_publish_blocked_when_snapshot_drift(self) -> None:
        pid = self.app.proposals.create_proposal("p", self.cn, self.de, "甲", self.full_items())
        self.app.proposals.submit_for_review(pid, "甲")
        self.app.proposals.signoff(pid, "A", "王", 1)
        # 第二方签署前，底层证据要求被篡改 → 快照复算不一致，拒绝发布
        self.app.conn.execute("UPDATE evidence_requirements SET requirement='被篡改' WHERE code='TH'")
        self.app.conn.commit()
        with self.assertRaises(SigningConditionError):
            self.app.proposals.signoff(pid, "B", "Müller", 1)
