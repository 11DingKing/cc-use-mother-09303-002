"""任意两版发布的比较与确定性复算。"""
from __future__ import annotations

import sqlite3

from ..domain.canon import canonical_dumps, content_hash
from ..domain.errors import NotFoundError
from ..domain.enums import Decision, PublishKind
from .standards import StandardService


class ComparisonService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.standards = StandardService(conn)

    def _publish_payload(self, publish_id: int) -> dict:
        """从冻结发布行重建规范化映射载荷（与发布时的快照结构一致）。"""
        row = self.conn.execute("SELECT * FROM publishes WHERE id=?", (publish_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"发布版本不存在：{publish_id}")
        if row["kind"] == PublishKind.WITHDRAWAL.value:
            return {"kind": PublishKind.WITHDRAWAL.value, "publish_no": row["publish_no"]}

        def unit_ref(standard_id: int) -> dict:
            s = self.conn.execute(
                "SELECT code, version_no, content_hash FROM standards WHERE id=?", (standard_id,)
            ).fetchone()
            return {"code": s["code"], "version_no": s["version_no"], "content_hash": s["content_hash"]}

        src_ref = unit_ref(row["source_standard_id"])
        tgt_ref = unit_ref(row["target_standard_id"])

        items = []
        for line in self.conn.execute(
            "SELECT * FROM publish_lines WHERE publish_id=? ORDER BY ordinal", (publish_id,)).fetchall():
            su = self.conn.execute("SELECT code FROM units WHERE id=?", (line["source_unit_id"],)).fetchone()
            tu = self.conn.execute("SELECT code FROM units WHERE id=?", (line["target_unit_id"],)).fetchone() \
                if line["target_unit_id"] else None
            gaps = []
            for g in self.conn.execute(
                "SELECT * FROM publish_line_gaps WHERE publish_line_id=? ORDER BY id",
                (line["id"],)).fetchall():
                def ev(eid):
                    if eid is None:
                        return None
                    return self.conn.execute(
                        "SELECT code FROM evidence_requirements WHERE id=?", (eid,)).fetchone()["code"]
                gaps.append({
                    "gap_kind": g["gap_kind"],
                    "source_evidence_code": ev(g["source_evidence_id"]),
                    "target_evidence_code": ev(g["target_evidence_id"]),
                    "description": g["description"],
                })
            items.append({
                "source_unit_code": su["code"],
                "target_unit_code": tu["code"] if tu else None,
                "decision": line["decision"],
                "rationale": line["rationale"],
                "gaps": sorted(gaps, key=lambda x: (x["gap_kind"], x["source_evidence_code"] or "",
                                                    x["target_evidence_code"] or "", x["description"])),
            })
        return {
            "source": src_ref,
            "target": tgt_ref,
            "items": sorted(items, key=lambda x: x["source_unit_code"]),
        }

    def recompute_publish_hash(self, publish_id: int) -> dict:
        row = self.conn.execute("SELECT mapping_hash FROM publishes WHERE id=?", (publish_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"发布版本不存在：{publish_id}")
        payload = self._publish_payload(publish_id)
        digest = content_hash(payload)
        return {
            "publish_id": publish_id,
            "stored_hash": row["mapping_hash"],
            "recomputed_hash": digest,
            "matches": digest == row["mapping_hash"],
            "payload": payload,
        }

    def compare(self, publish_a_id: int, publish_b_id: int) -> dict:
        """逐行比较任意两版：新增/移除/变更/不变，含结论与差距差异。"""
        pa = self._publish_payload(publish_a_id)
        pb = self._publish_payload(publish_b_id)
        ra = self.conn.execute("SELECT id, publish_no, kind, released_at FROM publishes WHERE id=?",
                               (publish_a_id,)).fetchone()
        rb = self.conn.execute("SELECT id, publish_no, kind, released_at FROM publishes WHERE id=?",
                               (publish_b_id,)).fetchone()

        if pa.get("kind") == PublishKind.WITHDRAWAL.value or pb.get("kind") == PublishKind.WITHDRAWAL.value:
            return {
                "a": dict(ra), "b": dict(rb),
                "comparable": False,
                "reason": "撤销版本不包含映射行，仅比较其与前版的有效状态",
                "a_kind": ra["kind"], "b_kind": rb["kind"],
            }

        a_items = {i["source_unit_code"]: i for i in pa["items"]}
        b_items = {i["source_unit_code"]: i for i in pb["items"]}
        added, removed, changed, unchanged = [], [], [], []
        for code in sorted(a_items.keys() | b_items.keys()):
            ia, ib = a_items.get(code), b_items.get(code)
            if ia is None:
                added.append({"source_unit_code": code, "now": ib})
            elif ib is None:
                removed.append({"source_unit_code": code, "before": ia})
            elif canonical_dumps(ia) == canonical_dumps(ib):
                unchanged.append(code)
            else:
                changed.append({
                    "source_unit_code": code,
                    "before_decision": ia["decision"],
                    "after_decision": ib["decision"],
                    "decision_changed": ia["decision"] != ib["decision"],
                    "target_changed": ia["target_unit_code"] != ib["target_unit_code"],
                    "gaps_before": ia["gaps"],
                    "gaps_after": ib["gaps"],
                    "rationale_changed": ia["rationale"] != ib["rationale"],
                })

        counts = lambda items: {d.value: sum(1 for i in items if i["decision"] == d.value)
                                for d in Decision}
        return {
            "a": dict(ra), "b": dict(rb), "comparable": True,
            "standard_versions": {"a": pa.get("source"), "b": pb.get("source")},
            "added": added, "removed": removed, "changed": changed, "unchanged": unchanged,
            "summary": {
                "added": len(added), "removed": len(removed),
                "changed": len(changed), "unchanged": len(unchanged),
                "full_before": counts(pa["items"])[Decision.FULL.value],
                "full_after": counts(pb["items"])[Decision.FULL.value],
                "partial_before": counts(pa["items"])[Decision.PARTIAL.value],
                "partial_after": counts(pb["items"])[Decision.PARTIAL.value],
                "none_before": counts(pa["items"])[Decision.NONE.value],
                "none_after": counts(pb["items"])[Decision.NONE.value],
                "hash_a": content_hash(pa), "hash_b": content_hash(pb),
            },
        }
