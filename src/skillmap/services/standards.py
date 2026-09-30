"""标准版本服务：各国标准版本导入、生效冻结、换版与能力图谱校验。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from ..domain.canon import canonical_dumps, content_hash
from ..domain.enums import RelationKind, StandardStatus
from ..domain.errors import DomainValidationError, NotFoundError, WorkflowError

VALID_EVIDENCE_KINDS = {"PRACTICAL", "THEORY", "DOCUMENT", "OBSERVATION"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StandardService:
    def __init__(self, conn: sqlite3.Connection, write_lock: threading.RLock | None = None):
        self.conn = conn
        self.write_lock = write_lock or threading.RLock()

    # ── 管辖区 ─────────────────────────────────────────────────────────

    def create_jurisdiction(self, code: str, name: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO jurisdictions(code, name, created_at) VALUES (?,?,?)",
            (code, name, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ── 校验 ───────────────────────────────────────────────────────────

    def _validate_graph(self, payload: dict) -> list[dict]:
        errors: list[str] = []
        for field in ("code", "title", "version_no", "publisher", "units"):
            if not payload.get(field):
                errors.append(f"标准缺少字段：{field}")
        if errors:
            raise DomainValidationError(errors)

        units = payload["units"]
        if not isinstance(units, list) or not units:
            raise DomainValidationError("标准至少包含一个能力单元")

        codes: set[str] = set()
        norm_units: list[dict] = []
        for i, u in enumerate(units):
            prefix = f"能力单元[{i}]"
            for field in ("code", "title", "level_label", "scope"):
                if not u.get(field):
                    errors.append(f"{prefix} 缺少字段：{field}")
            code = u.get("code")
            if code in codes:
                errors.append(f"{prefix} 能力单元编码重复：{code}")
            codes.add(code)

            evidence = u.get("evidence", [])
            ev_codes: set[str] = set()
            norm_ev: list[dict] = []
            for j, e in enumerate(evidence):
                for field in ("code", "title", "evidence_kind", "requirement"):
                    if not e.get(field):
                        errors.append(f"{prefix} 考核证据[{j}] 缺少字段：{field}")
                if e.get("evidence_kind") and e["evidence_kind"] not in VALID_EVIDENCE_KINDS:
                    errors.append(f"{prefix} 考核证据[{j}] 证据类型非法：{e['evidence_kind']}")
                if e.get("code") in ev_codes:
                    errors.append(f"{prefix} 考核证据编码重复：{e.get('code')}")
                ev_codes.add(e.get("code"))
                norm_ev.append(
                    {
                        "code": e.get("code"),
                        "title": e.get("title"),
                        "evidence_kind": e.get("evidence_kind"),
                        "requirement": e.get("requirement"),
                    }
                )

            rels: dict[str, list[str]] = {RelationKind.REQUIRES.value: [], RelationKind.RECOMMENDS.value: []}
            for kind_field, kind in (("requires", RelationKind.REQUIRES), ("recommends", RelationKind.RECOMMENDS)):
                for ref in u.get(kind_field, []) or []:
                    if not isinstance(ref, str) or not ref:
                        errors.append(f"{prefix} 前置关系编码非法：{ref!r}")
                    rels[kind.value].append(ref)

            norm_units.append(
                {
                    "code": code,
                    "title": u.get("title"),
                    "level_label": u.get("level_label"),
                    "scope": u.get("scope"),
                    "evidence": norm_ev,
                    "requires": sorted(rels[RelationKind.REQUIRES.value]),
                    "recommends": sorted(rels[RelationKind.RECOMMENDS.value]),
                }
            )

        # 前置关系必须指向本标准内单元，且不得成环
        by_code = {u["code"]: u for u in norm_units}
        for u in norm_units:
            for ref in u["requires"] + u["recommends"]:
                if ref not in by_code:
                    errors.append(f"能力单元 {u['code']} 的前置 {ref} 不存在于本标准")
                if ref == u["code"]:
                    errors.append(f"能力单元 {u['code']} 不能以前置关系指向自身")
        if not errors:
            self._assert_acyclic(by_code)
        if errors:
            raise DomainValidationError(errors)
        return norm_units

    @staticmethod
    def _assert_acyclic(by_code: dict[str, dict]) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {c: WHITE for c in by_code}

        def visit(code: str, stack: list[str]) -> None:
            color[code] = GRAY
            for ref in by_code[code]["requires"]:
                if color[ref] == GRAY:
                    cycle = " -> ".join(stack + [ref])
                    raise DomainValidationError(f"前置关系存在环：{cycle}")
                if color[ref] == WHITE:
                    visit(ref, stack + [ref])
            color[code] = BLACK

        for c in by_code:
            if color[c] == WHITE:
                visit(c, [c])

    def _graph_payload(self, standard_row: sqlite3.Row, units: list[dict]) -> dict:
        return {
            "code": standard_row["code"],
            "title": standard_row["title"],
            "version_no": standard_row["version_no"],
            "publisher": standard_row["publisher"],
            "units": units,
        }

    def _write_graph(self, standard_id: int, units: list[dict]) -> None:
        for ordinal, u in enumerate(units):
            cur = self.conn.execute(
                """INSERT INTO units(standard_id, code, title, level_label, scope, ordinal)
                   VALUES (?,?,?,?,?,?)""",
                (standard_id, u["code"], u["title"], u["level_label"], u["scope"], ordinal),
            )
            unit_id = int(cur.lastrowid)
            for j, e in enumerate(u["evidence"]):
                self.conn.execute(
                    """INSERT INTO evidence_requirements(unit_id, code, title, evidence_kind, requirement, ordinal)
                       VALUES (?,?,?,?,?,?)""",
                    (unit_id, e["code"], e["title"], e["evidence_kind"], e["requirement"], j),
                )
            for kind_field, kind in (("requires", RelationKind.REQUIRES), ("recommends", RelationKind.RECOMMENDS)):
                for ref in u[kind_field]:
                    self.conn.execute(
                        "INSERT INTO prerequisites(unit_id, prerequisite_unit_id, relation_kind) VALUES (?,?,?)",
                        (unit_id, self._unit_id(standard_id, ref), kind.value),
                    )

    def _unit_id(self, standard_id: int, code: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM units WHERE standard_id=? AND code=?", (standard_id, code)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"能力单元不存在：{code}")
        return int(row["id"])

    # ── 导入 / 生效 / 换版 ─────────────────────────────────────────────

    def import_standard(self, jurisdiction_code: str, payload: dict) -> int:
        jr = self.conn.execute(
            "SELECT id FROM jurisdictions WHERE code=?", (jurisdiction_code,)
        ).fetchone()
        if jr is None:
            raise NotFoundError(f"管辖区不存在：{jurisdiction_code}")
        jurisdiction_id = int(jr["id"])

        units = self._validate_graph(payload)
        status = StandardStatus.EFFECTIVE.value if payload.get("effective_at") else StandardStatus.DRAFT.value
        now = _now()
        try:
            cur = self.conn.execute(
                """INSERT INTO standards(jurisdiction_id, code, title, version_no, status, publisher,
                                          effective_at, published_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    jurisdiction_id, payload["code"], payload["title"], payload["version_no"],
                    status, payload["publisher"], payload.get("effective_at"),
                    now if status == StandardStatus.EFFECTIVE.value else None, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DomainValidationError(f"标准版本已存在：{payload['code']}@{payload['version_no']}") from exc
        standard_id = int(cur.lastrowid)
        self._write_graph(standard_id, units)

        if status == StandardStatus.EFFECTIVE.value:
            self._freeze(standard_id)
        self.conn.commit()
        return standard_id

    def mark_effective(self, standard_id: int, effective_at: str | None = None) -> None:
        row = self._require_standard(standard_id)
        if row["status"] != StandardStatus.DRAFT.value:
            raise WorkflowError(f"仅草稿标准可生效，当前状态：{row['status']}")
        at = effective_at or _now()
        self.conn.execute(
            "UPDATE standards SET status=?, effective_at=?, published_at=? WHERE id=?",
            (StandardStatus.EFFECTIVE.value, at, at, standard_id),
        )
        # 同一标准的更早生效版本自动进入 SUPERSEDED（标准换版链）
        self.conn.execute(
            """UPDATE standards SET status=?, superseded_by_id=?
               WHERE id<>? AND code=? AND jurisdiction_id=? AND status=?""",
            (StandardStatus.SUPERSEDED.value, standard_id, standard_id, row["code"],
             row["jurisdiction_id"], StandardStatus.EFFECTIVE.value),
        )
        self._freeze(standard_id)
        self.conn.commit()

    def _freeze(self, standard_id: int) -> None:
        """计算并固化能力图谱内容摘要；生效后内容不可变。"""
        row = self._require_standard(standard_id)
        units = self.load_graph(standard_id)
        digest = content_hash(self._graph_payload(row, units))
        self.conn.execute("UPDATE standards SET content_hash=? WHERE id=?", (digest, standard_id))

    def new_version_draft(self, parent_standard_id: int, payload: dict) -> int:
        """基于已生效版本起草新版（标准换版）；新版生效前父版保持有效。"""
        parent = self._require_standard(parent_standard_id)
        if parent["status"] != StandardStatus.EFFECTIVE.value:
            raise WorkflowError("只能基于当前生效版本起草新版")
        if payload.get("code") != parent["code"]:
            raise DomainValidationError("换版不得改变标准编码，请改用新立标准")
        draft = self.conn.execute(
            "SELECT id FROM standards WHERE code=? AND jurisdiction_id=? AND status=?",
            (parent["code"], parent["jurisdiction_id"], StandardStatus.DRAFT.value),
        ).fetchone()
        if draft is not None:
            raise WorkflowError("该标准已存在未生效的草稿版本，请先处理")
        payload = {**payload, "effective_at": None}
        units = self._validate_graph(payload)
        now = _now()
        cur = self.conn.execute(
            """INSERT INTO standards(jurisdiction_id, code, title, version_no, status, publisher, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (parent["jurisdiction_id"], payload["code"], payload["title"], payload["version_no"],
             StandardStatus.DRAFT.value, payload["publisher"], now),
        )
        new_id = int(cur.lastrowid)
        self._write_graph(new_id, units)
        self.conn.commit()
        return new_id

    # ── 查询 ───────────────────────────────────────────────────────────

    def _require_standard(self, standard_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM standards WHERE id=?", (standard_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"标准版本不存在：{standard_id}")
        return row

    def load_graph(self, standard_id: int) -> list[dict]:
        units = self.conn.execute(
            "SELECT * FROM units WHERE standard_id=? ORDER BY ordinal, id", (standard_id,)
        ).fetchall()
        result = []
        for u in units:
            evidence = [
                dict(e)
                for e in self.conn.execute(
                    "SELECT * FROM evidence_requirements WHERE unit_id=? ORDER BY ordinal, id", (u["id"],)
                ).fetchall()
            ]
            req, rec = [], []
            for r in self.conn.execute(
                """SELECT pu.code AS ref, p.relation_kind AS kind FROM prerequisites p
                   JOIN units pu ON pu.id = p.prerequisite_unit_id
                   WHERE p.unit_id=?""",
                (u["id"],),
            ).fetchall():
                (req if r["kind"] == RelationKind.REQUIRES.value else rec).append(r["ref"])
            result.append(
                {
                    "code": u["code"],
                    "title": u["title"],
                    "level_label": u["level_label"],
                    "scope": u["scope"],
                    "evidence": [
                        {
                            "code": e["code"],
                            "title": e["title"],
                            "evidence_kind": e["evidence_kind"],
                            "requirement": e["requirement"],
                        }
                        for e in evidence
                    ],
                    "requires": sorted(req),
                    "recommends": sorted(rec),
                }
            )
        return result

    def get_standard(self, standard_id: int) -> dict:
        row = self._require_standard(standard_id)
        jr = self.conn.execute("SELECT code, name FROM jurisdictions WHERE id=?", (row["jurisdiction_id"],)).fetchone()
        return {
            "id": row["id"],
            "jurisdiction": {"code": jr["code"], "name": jr["name"]},
            "code": row["code"],
            "title": row["title"],
            "version_no": row["version_no"],
            "status": row["status"],
            "publisher": row["publisher"],
            "effective_at": row["effective_at"],
            "content_hash": row["content_hash"],
            "superseded_by_id": row["superseded_by_id"],
            "units": self.load_graph(standard_id),
        }

    def list_standards(self, code: str | None = None) -> list[dict]:
        sql = "SELECT id FROM standards"
        args: tuple = ()
        if code:
            sql += " WHERE code=?"
            args = (code,)
        sql += " ORDER BY code, version_no"
        return [self.get_standard(r["id"]) for r in self.conn.execute(sql, args).fetchall()]

    def verify_content_hash(self, standard_id: int) -> bool:
        """用当前存储的图谱重新复算摘要，与冻结值比对（防篡改/复算）。"""
        row = self._require_standard(standard_id)
        recomputed = content_hash(self._graph_payload(row, self.load_graph(standard_id)))
        return recomputed == row["content_hash"]
