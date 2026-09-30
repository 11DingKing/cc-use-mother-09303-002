"""发布版本与历史证书服务。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from ..domain.canon import canonical_dumps, content_hash
from ..domain.enums import PublishKind
from ..domain.errors import DomainValidationError, NotFoundError, WorkflowError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PublishService:
    def __init__(self, conn: sqlite3.Connection, write_lock: threading.RLock | None = None):
        self.conn = conn
        self.write_lock = write_lock or threading.RLock()

    def _require(self, publish_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM publishes WHERE id=?", (publish_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"发布版本不存在：{publish_id}")
        return row

    def get_publish(self, publish_id: int) -> dict:
        row = self._require(publish_id)
        lines = []
        for line in self.conn.execute(
            "SELECT * FROM publish_lines WHERE publish_id=? ORDER BY ordinal", (publish_id,)).fetchall():
            src = self.conn.execute(
                """SELECT s.code AS standard_code, s.version_no AS version_no, u.code AS unit_code,
                          u.title AS title, u.level_label AS level_label
                   FROM units u JOIN standards s ON s.id=u.standard_id WHERE u.id=?""",
                (line["source_unit_id"],)).fetchone()
            tgt = None
            if line["target_unit_id"]:
                r = self.conn.execute(
                    """SELECT s.code AS standard_code, s.version_no AS version_no, u.code AS unit_code,
                              u.title AS title, u.level_label AS level_label
                       FROM units u JOIN standards s ON s.id=u.standard_id WHERE u.id=?""",
                    (line["target_unit_id"],)).fetchone()
                tgt = dict(r)
            gaps = []
            for g in self.conn.execute(
                "SELECT * FROM publish_line_gaps WHERE publish_line_id=?", (line["id"],)).fetchall():
                gaps.append({
                    "gap_kind": g["gap_kind"],
                    "source_evidence_code": self._ev_code(g["source_evidence_id"]),
                    "target_evidence_code": self._ev_code(g["target_evidence_id"]),
                    "description": g["description"],
                })
            lines.append({
                "source": dict(src),
                "target": tgt,
                "decision": line["decision"],
                "rationale": line["rationale"],
                "gaps": gaps,
            })
        return {
            "id": row["id"],
            "publish_no": row["publish_no"],
            "proposal_id": row["proposal_id"],
            "revision_no": row["revision_no"],
            "kind": row["kind"],
            "mapping_hash": row["mapping_hash"],
            "prev_publish_id": row["prev_publish_id"],
            "released_at": row["released_at"],
            "superseded_at": row["superseded_at"],
            "source_standard_id": row["source_standard_id"],
            "target_standard_id": row["target_standard_id"],
            "lines": lines,
        }

    def _ev_code(self, evidence_id: int | None) -> str | None:
        if evidence_id is None:
            return None
        r = self.conn.execute("SELECT code FROM evidence_requirements WHERE id=?", (evidence_id,)).fetchone()
        return r["code"] if r else None

    def history(self, source_standard_id: int, target_standard_id: int) -> list[dict]:
        """该标准对的完整发布链（含映射、撤销，按时间排序）。"""
        rows = self.conn.execute(
            """SELECT id FROM publishes
               WHERE source_standard_id=? AND target_standard_id=?
               ORDER BY id""",
            (source_standard_id, target_standard_id),
        ).fetchall()
        return [self.get_publish(r["id"]) for r in rows]

    def current(self, source_standard_id: int, target_standard_id: int) -> dict | None:
        """最新发布；若为撤销版本，则当前不存在有效互认。"""
        row = self.conn.execute(
            """SELECT * FROM publishes
               WHERE source_standard_id=? AND target_standard_id=?
               ORDER BY id DESC LIMIT 1""",
            (source_standard_id, target_standard_id),
        ).fetchone()
        return self.get_publish(row["id"]) if row else None

    # ── 历史证书：签发时钉住当时采用的发布版本与摘要 ───────────────────

    def issue_certificate(self, certificate_no: str, holder: str, source_standard_id: int,
                          grade_label: str, publish_id: int, issuer: str) -> int:
        pub = self._require(publish_id)
        if pub["kind"] != PublishKind.MAPPING.value:
            raise WorkflowError("证书只能依据映射发布版本签发，不能依据撤销版本")
        if pub["source_standard_id"] != source_standard_id:
            raise DomainValidationError("证书标准与发布版本的来源标准不一致")
        if pub["superseded_at"] is not None:
            # 历史版本仍可签发补发/换证副本，但必须显式钉住旧版；此处记录但不禁止
            pass
        try:
            cur = self.conn.execute(
                """INSERT INTO certificates(certificate_no, holder, source_standard_id, grade_label,
                                            publish_id, mapping_hash_at_issue, issuer, issued_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (certificate_no, holder, source_standard_id, grade_label, publish_id,
                 pub["mapping_hash"], issuer, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DomainValidationError(f"证书编号重复：{certificate_no}") from exc
        self.conn.commit()
        return int(cur.lastrowid)

    def _successor_chain_head(self, publish_id: int) -> int:
        """沿 prev_publish_id 后继链找到该发布所属互认链的最新版本 id。"""
        row = self.conn.execute(
            """WITH RECURSIVE chain(id) AS (
                   SELECT ?
                   UNION ALL
                   SELECT p.id FROM publishes p JOIN chain c ON p.prev_publish_id = c.id
               )
               SELECT id FROM chain ORDER BY id DESC LIMIT 1""",
            (publish_id,),
        ).fetchone()
        return int(row["id"])

    def get_certificate(self, certificate_id_or_no: str | int) -> dict:
        if isinstance(certificate_id_or_no, int) or str(certificate_id_or_no).isdigit():
            row = self.conn.execute("SELECT * FROM certificates WHERE id=?",
                                    (int(certificate_id_or_no),)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM certificates WHERE certificate_no=?",
                                    (certificate_id_or_no,)).fetchone()
        if row is None:
            raise NotFoundError(f"证书不存在：{certificate_id_or_no}")
        head_id = self._successor_chain_head(row["publish_id"])
        head = self._require(head_id)
        return {
            "id": row["id"],
            "certificate_no": row["certificate_no"],
            "holder": row["holder"],
            "grade_label": row["grade_label"],
            "issuer": row["issuer"],
            "issued_at": row["issued_at"],
            "source_standard_id": row["source_standard_id"],
            "adopted_publish": {"id": row["publish_id"],
                                "mapping_hash_at_issue": row["mapping_hash_at_issue"]},
            "latest_publish": {"id": head_id, "publish_no": head["publish_no"], "kind": head["kind"]},
            # 沿后继链比较：换版/部分互认调整/撤销都会使“当前采用版本”不同于签发时
            "current_mapping_changed": row["mapping_hash_at_issue"] != head["mapping_hash"],
        }

    def list_certificates(self) -> list[dict]:
        rows = self.conn.execute("SELECT id FROM certificates ORDER BY id").fetchall()
        return [self.get_certificate(r["id"]) for r in rows]

    # ── 复算 ───────────────────────────────────────────────────────────

    def recompute_hash(self, publish_id: int) -> dict:
        """从冻结的发布行重新构建规范化载荷并复算摘要，与固化值比对。"""
        from .comparison import ComparisonService  # 局部导入避免循环
        return ComparisonService(self.conn).recompute_publish_hash(publish_id)
