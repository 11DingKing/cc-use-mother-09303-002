"""映射提案服务：逐项覆盖/差距/依据、修订版本、双方会签与发布。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from ..domain.canon import canonical_dumps, content_hash
from ..domain.enums import Decision, GapKind, ProposalStatus, PublishKind, SignParty
from ..domain.errors import (
    ConcurrentSignoffError,
    DomainValidationError,
    NotFoundError,
    SigningConditionError,
    WorkflowError,
)
from .standards import StandardService

VALID_DECISIONS = {d.value for d in Decision}
VALID_GAP_KINDS = {g.value for g in GapKind}
VALID_PARTIES = {p.value for p in SignParty}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seq(conn: sqlite3.Connection, table: str, col: str) -> str:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return f"{int(row['n']) + 1:04d}"


class ProposalService:
    def __init__(self, conn: sqlite3.Connection, write_lock: threading.RLock | None = None):
        self.conn = conn
        self.write_lock = write_lock or threading.RLock()
        self.standards = StandardService(conn, self.write_lock)

    # ── 内部工具 ───────────────────────────────────────────────────────

    def _require(self, proposal_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"映射提案不存在：{proposal_id}")
        return row

    def _event(self, proposal_id: int, kind: str, actor: str, payload: dict | None = None) -> None:
        n = self.conn.execute("SELECT COUNT(*) AS n FROM proposal_events WHERE proposal_id=?", (proposal_id,)).fetchone()["n"]
        self.conn.execute(
            "INSERT INTO proposal_events(proposal_id, seq, event_type, actor, payload_json, at) VALUES (?,?,?,?,?,?)",
            (proposal_id, n + 1, kind, actor, canonical_dumps(payload or {}), _now()),
        )

    def _unit_map(self, standard_id: int) -> dict[str, int]:
        return {r["code"]: r["id"] for r in self.conn.execute(
            "SELECT id, code FROM units WHERE standard_id=?", (standard_id,))}

    def _evidence_map(self, unit_id: int) -> dict[str, int]:
        return {r["code"]: r["id"] for r in self.conn.execute(
            "SELECT id, code FROM evidence_requirements WHERE unit_id=?", (unit_id,))}

    def _effective(self, standard_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM standards WHERE id=?", (standard_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"标准版本不存在：{standard_id}")
        if row["status"] != "EFFECTIVE":
            raise WorkflowError(f"提案只能引用生效标准，{row['code']}@{row['version_no']} 当前为 {row['status']}")
        return row

    # ── 规范化与摘要 ───────────────────────────────────────────────────

    def _normalize_items(self, source_id: int, target_id: int, items: list[dict],
                         *, exhaustive: bool) -> list[dict]:
        errors: list[str] = []
        src_units, tgt_units = self._unit_map(source_id), self._unit_map(target_id)

        seen: set[str] = set()
        norm: list[dict] = []
        for i, item in enumerate(items):
            prefix = f"映射项[{i}]"
            src_code = item.get("source_unit_code")
            tgt_code = item.get("target_unit_code")
            decision = item.get("decision")
            rationale = (item.get("rationale") or "").strip()

            if src_code not in src_units:
                errors.append(f"{prefix} 来源能力单元不存在：{src_code}")
            if src_code in seen:
                errors.append(f"{prefix} 来源能力单元重复映射：{src_code}")
            seen.add(src_code)
            if decision not in VALID_DECISIONS:
                errors.append(f"{prefix} 互认结论非法：{decision}")
            if not rationale:
                errors.append(f"{prefix} 必须逐项给出依据（rationale）")
            if decision in (Decision.FULL.value, Decision.PARTIAL.value) and not tgt_code:
                errors.append(f"{prefix} {decision} 必须指定目标能力单元")
            if tgt_code and tgt_code not in tgt_units:
                errors.append(f"{prefix} 目标能力单元不存在：{tgt_code}")

            gaps = item.get("gaps", []) or []
            norm_gaps: list[dict] = []
            if decision == Decision.FULL.value and gaps:
                errors.append(f"{prefix} 完全互认不得登记差距项")
            if decision == Decision.PARTIAL.value and not gaps:
                errors.append(f"{prefix} 部分互认必须逐项列明差距")
            src_ev = self._evidence_map(src_units[src_code]) if src_code in src_units else {}
            tgt_ev = self._evidence_map(tgt_units[tgt_code]) if tgt_code and tgt_code in tgt_units else {}
            for j, g in enumerate(gaps):
                gprefix = f"{prefix} 差距[{j}]"
                kind = g.get("gap_kind")
                if kind not in VALID_GAP_KINDS:
                    errors.append(f"{gprefix} 差距类型非法：{kind}")
                if not (g.get("description") or "").strip():
                    errors.append(f"{gprefix} 差距描述不能为空")
                se, te = g.get("source_evidence_code"), g.get("target_evidence_code")
                if se is not None and se not in src_ev:
                    errors.append(f"{gprefix} 来源考核证据不存在：{se}")
                if te is not None and te not in tgt_ev:
                    errors.append(f"{gprefix} 目标考核证据不存在：{te}")
                norm_gaps.append({
                    "gap_kind": kind,
                    "source_evidence_code": se,
                    "target_evidence_code": te,
                    "description": g["description"].strip(),
                })

            norm.append({
                "source_unit_code": src_code,
                "target_unit_code": tgt_code,
                "decision": decision,
                "rationale": rationale,
                "gaps": sorted(norm_gaps, key=lambda g: (g["gap_kind"], g["source_evidence_code"] or "",
                                                         g["target_evidence_code"] or "", g["description"])),
            })

        if exhaustive:
            missing = sorted(set(src_units) - seen)
            if missing:
                errors.append("以下来源能力单元尚未逐项映射，禁止提交：" + "、".join(missing))
        if errors:
            raise DomainValidationError(errors)
        return sorted(norm, key=lambda x: x["source_unit_code"])

    def _snapshot_payload(self, src_row: sqlite3.Row, tgt_row: sqlite3.Row, items: list[dict]) -> dict:
        return {
            "source": {"code": src_row["code"], "version_no": src_row["version_no"],
                       "content_hash": src_row["content_hash"]},
            "target": {"code": tgt_row["code"], "version_no": tgt_row["version_no"],
                       "content_hash": tgt_row["content_hash"]},
            "items": items,
        }

    # ── 创建 / 修订 / 提交 ─────────────────────────────────────────────

    def create_proposal(self, title: str, source_standard_id: int, target_standard_id: int,
                        creator: str, items: list[dict]) -> int:
        if source_standard_id == target_standard_id:
            raise DomainValidationError("来源与目标标准不能相同")
        src, tgt = self._effective(source_standard_id), self._effective(target_standard_id)
        if src["jurisdiction_id"] == tgt["jurisdiction_id"]:
            raise DomainValidationError("互认映射必须跨越两个合作方管辖区")
        norm = self._normalize_items(source_standard_id, target_standard_id, items,
                                     exhaustive=False)
        digest = content_hash(self._snapshot_payload(src, tgt, norm))
        no = "MAP-" + _seq(self.conn, "proposals", "id")
        cur = self.conn.execute(
            """INSERT INTO proposals(proposal_no, title, source_standard_id, target_standard_id,
                                     status, creator, revision_no, snapshot_hash, created_at)
               VALUES (?,?,?,?,?,?,1,?,?)""",
            (no, title, source_standard_id, target_standard_id, ProposalStatus.DRAFT.value,
             creator, digest, _now()),
        )
        pid = int(cur.lastrowid)
        self._write_items(pid, 1, norm, self._unit_map(source_standard_id),
                          self._unit_map(target_standard_id))
        self.conn.execute(
            """INSERT INTO proposal_revisions(proposal_id, revision_no, snapshot_hash, change_note,
                                              created_by, created_at)
               VALUES (?,1,?,'初稿',?,?)""",
            (pid, digest, creator, _now()),
        )
        self._event(pid, "CREATED", creator, {"proposal_no": no, "revision_no": 1})
        self.conn.commit()
        return pid

    def revise(self, proposal_id: int, items: list[dict], actor: str, change_note: str = "") -> int:
        p = self._require(proposal_id)
        if p["status"] not in (ProposalStatus.DRAFT.value,):
            raise WorkflowError(f"仅起草中的提案可修订，当前状态：{p['status']}")
        new_rev = p["revision_no"] + 1
        norm = self._normalize_items(p["source_standard_id"], p["target_standard_id"], items,
                                     exhaustive=False)
        src = self._effective(p["source_standard_id"])
        tgt = self._effective(p["target_standard_id"])
        digest = content_hash(self._snapshot_payload(src, tgt, norm))
        self._write_items(proposal_id, new_rev, norm,
                          self._unit_map(p["source_standard_id"]),
                          self._unit_map(p["target_standard_id"]))
        self.conn.execute(
            """INSERT INTO proposal_revisions(proposal_id, revision_no, snapshot_hash, change_note,
                                              created_by, created_at)
               VALUES (?,?,?,?,?,?)""",
            (proposal_id, new_rev, digest, change_note or f"第 {new_rev} 版修订", actor, _now()),
        )
        self.conn.execute(
            "UPDATE proposals SET revision_no=?, snapshot_hash=?, decision_seq=decision_seq+1 WHERE id=?",
            (new_rev, digest, proposal_id),
        )
        self._event(proposal_id, "REVISED", actor, {"revision_no": new_rev, "note": change_note})
        self.conn.commit()
        return new_rev

    def submit_for_review(self, proposal_id: int, actor: str) -> None:
        p = self._require(proposal_id)
        if p["status"] != ProposalStatus.DRAFT.value:
            raise WorkflowError(f"仅起草中的提案可提交会审，当前状态：{p['status']}")
        items = self._load_items(proposal_id, p["revision_no"])
        # 提交时强制完整性复核：所有来源单元逐项覆盖
        norm = self._normalize_items(p["source_standard_id"], p["target_standard_id"], items,
                                     exhaustive=True)
        src = self._effective(p["source_standard_id"])
        tgt = self._effective(p["target_standard_id"])
        digest = content_hash(self._snapshot_payload(src, tgt, norm))
        self.conn.execute(
            "UPDATE proposals SET status=?, snapshot_hash=?, submitted_at=? WHERE id=?",
            (ProposalStatus.IN_REVIEW.value, digest, _now(), proposal_id),
        )
        self._event(proposal_id, "SUBMITTED", actor, {"revision_no": p["revision_no"]})
        self.conn.commit()

    def request_changes(self, proposal_id: int, actor: str, note: str = "") -> None:
        """会审中任一方提出修改：退回起草，本修订会签全部作废，需重新走版本。"""
        p = self._require(proposal_id)
        if p["status"] != ProposalStatus.IN_REVIEW.value:
            raise WorkflowError("仅会审中的提案可退回修改")
        self.conn.execute("DELETE FROM signoffs WHERE proposal_id=? AND revision_no=?",
                          (proposal_id, p["revision_no"]))
        self.conn.execute("UPDATE proposals SET status=? WHERE id=?",
                          (ProposalStatus.DRAFT.value, proposal_id))
        self._event(proposal_id, "REOPENED", actor, {"revision_no": p["revision_no"], "note": note})
        self.conn.commit()

    # ── 会签与发布（并发安全）──────────────────────────────────────────

    def signoff(self, proposal_id: int, party: str, signer: str, expected_revision: int) -> dict:
        """一方专家组签署。两方齐备时在同一临界区内原子发布。

        并发会签通过共享写锁串行化：后到线程在锁内重新读取最新状态，
        重复签署或版本过期抛出 ConcurrentSignoffError，调用方必须重读后重试。
        """
        if party not in VALID_PARTIES:
            raise DomainValidationError(f"会签方非法：{party}")
        with self.write_lock:
            p = self.conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if p is None:
                raise NotFoundError(f"映射提案不存在：{proposal_id}")
            if p["status"] != ProposalStatus.IN_REVIEW.value:
                raise WorkflowError(f"仅会审中的提案可签署，当前状态：{p['status']}")
            if expected_revision != p["revision_no"]:
                raise ConcurrentSignoffError(
                    f"提案已更新到第 {p['revision_no']} 版，您持有的是第 {expected_revision} 版，请重读")
            dup = self.conn.execute(
                "SELECT 1 FROM signoffs WHERE proposal_id=? AND revision_no=? AND party=?",
                (proposal_id, p["revision_no"], party),
            ).fetchone()
            if dup is not None:
                raise ConcurrentSignoffError(f"{party} 方已完成签署，请勿重复提交")

            self.conn.execute(
                "INSERT INTO signoffs(proposal_id, revision_no, party, signer, signed_at) VALUES (?,?,?,?,?)",
                (proposal_id, p["revision_no"], party, signer, _now()),
            )
            self._event(proposal_id, "SIGNED", signer, {"revision_no": p["revision_no"], "party": party})

            parties = {r["party"] for r in self.conn.execute(
                "SELECT party FROM signoffs WHERE proposal_id=? AND revision_no=?",
                (proposal_id, p["revision_no"]))}
            published = None
            if parties == VALID_PARTIES:
                published = self._publish_locked(p, signer)
            self.conn.commit()
            return {"signed_party": party, "all_signed": parties == VALID_PARTIES,
                    "publish_id": published}

    def _publish_locked(self, p: sqlite3.Row, actor: str) -> int:
        """调用方已持有写锁；满足双方会签后固化不可变发布版本。"""
        revision_no = p["revision_no"]
        items = self._load_items(p["id"], revision_no)
        # 发布前最后一次确定性校验：完整性 + 标准仍生效 + 快照哈希复算一致
        norm = self._normalize_items(p["source_standard_id"], p["target_standard_id"], items,
                                     exhaustive=True)
        src = self._effective(p["source_standard_id"])
        tgt = self._effective(p["target_standard_id"])
        # 两侧标准的能力图谱必须与其冻结摘要一致（防底层篡改）
        if not self.standards.verify_content_hash(p["source_standard_id"]):
            raise SigningConditionError(f"来源标准 {src['code']}@{src['version_no']} 图谱复算不一致，禁止发布")
        if not self.standards.verify_content_hash(p["target_standard_id"]):
            raise SigningConditionError(f"目标标准 {tgt['code']}@{tgt['version_no']} 图谱复算不一致，禁止发布")
        expected_hash = content_hash(self._snapshot_payload(src, tgt, norm))
        if p["snapshot_hash"] != expected_hash:
            raise SigningConditionError("提案快照与生效标准图谱复算不一致，禁止发布")

        # 前驱版本：换版更新提案沿 based_on_publish_id 接续互认链；否则取同标准对上一版
        if p["based_on_publish_id"] is not None:
            prev = self.conn.execute(
                "SELECT * FROM publishes WHERE id=?", (p["based_on_publish_id"],)).fetchone()
        else:
            prev = self.conn.execute(
                """SELECT * FROM publishes
                   WHERE source_standard_id=? AND target_standard_id=?
                     AND kind IN ('MAPPING','WITHDRAWAL')
                   ORDER BY id DESC LIMIT 1""",
                (p["source_standard_id"], p["target_standard_id"]),
            ).fetchone()
        pub_no = "PUB-" + _seq(self.conn, "publishes", "id")
        now = _now()
        cur = self.conn.execute(
            """INSERT INTO publishes(publish_no, proposal_id, revision_no, kind,
                                     source_standard_id, target_standard_id, mapping_hash,
                                     prev_publish_id, published_by, released_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (pub_no, p["id"], revision_no, PublishKind.MAPPING.value,
             p["source_standard_id"], p["target_standard_id"], expected_hash,
             prev["id"] if prev else None, actor, now),
        )
        pub_id = int(cur.lastrowid)
        if prev is not None:
            self.conn.execute("UPDATE publishes SET superseded_at=? WHERE id=?", (now, prev["id"]))
            if prev["proposal_id"] != p["id"]:
                self.conn.execute("UPDATE proposals SET status=? WHERE id=?",
                                  (ProposalStatus.SUPERSEDED.value, prev["proposal_id"]))
        self._freeze_lines(pub_id, p["id"], revision_no, norm)
        self.conn.execute(
            "UPDATE proposals SET status=?, current_publish_id=? WHERE id=?",
            (ProposalStatus.PUBLISHED.value, pub_id, p["id"]),
        )
        self._event(p["id"], "PUBLISHED", actor,
                    {"publish_id": pub_id, "publish_no": pub_no, "revision_no": revision_no})
        return pub_id

    def _freeze_lines(self, publish_id: int, proposal_id: int, revision_no: int, items: list[dict]) -> None:
        p = self.conn.execute("SELECT source_standard_id, target_standard_id FROM proposals WHERE id=?",
                              (proposal_id,)).fetchone()
        src_units = self._unit_map(p["source_standard_id"])
        tgt_units = self._unit_map(p["target_standard_id"])
        for ordinal, item in enumerate(items):
            cur = self.conn.execute(
                """INSERT INTO publish_lines(publish_id, source_unit_id, target_unit_id, decision,
                                             rationale, ordinal)
                   VALUES (?,?,?,?,?,?)""",
                (publish_id, src_units[item["source_unit_code"]],
                 tgt_units.get(item["target_unit_code"]) if item["target_unit_code"] else None,
                 item["decision"], item["rationale"], ordinal),
            )
            line_id = int(cur.lastrowid)
            src_ev = self._evidence_map(src_units[item["source_unit_code"]])
            tgt_ev = self._evidence_map(tgt_units[item["target_unit_code"]]) if item["target_unit_code"] else {}
            for g in item["gaps"]:
                self.conn.execute(
                    """INSERT INTO publish_line_gaps(publish_line_id, source_evidence_id,
                                                     target_evidence_id, gap_kind, description)
                       VALUES (?,?,?,?,?)""",
                    (line_id,
                     src_ev.get(g["source_evidence_code"]) if g.get("source_evidence_code") else None,
                     tgt_ev.get(g["target_evidence_code"]) if g.get("target_evidence_code") else None,
                     g["gap_kind"], g["description"]),
                )

    # ── 撤回 ───────────────────────────────────────────────────────────

    def withdraw(self, proposal_id: int, actor: str, reason: str = "") -> dict:
        """起草/会签中：直接撤回提案；已发布：登记 WITHDRAWAL 新版本（互认撤销）。

        历史发布版本与已签发证书均不删除、不改写。
        """
        p = self._require(proposal_id)
        if p["status"] in (ProposalStatus.WITHDRAWN.value,):
            raise WorkflowError("提案已撤回")
        if p["status"] == ProposalStatus.PUBLISHED.value:
            pub_id = self._publish_withdrawal(p, actor, reason)
            self.conn.commit()
            return {"kind": PublishKind.WITHDRAWAL.value, "publish_id": pub_id}

        self.conn.execute("UPDATE proposals SET status=? WHERE id=?",
                          (ProposalStatus.WITHDRAWN.value, proposal_id))
        self._event(proposal_id, "WITHDRAWN", actor, {"reason": reason})
        self.conn.commit()
        return {"kind": "PROPOSAL_WITHDRAWN"}

    def _publish_withdrawal(self, p: sqlite3.Row, actor: str, reason: str) -> int:
        prev = self.conn.execute(
            "SELECT * FROM publishes WHERE id=?", (p["current_publish_id"],)).fetchone()
        now = _now()
        digest = content_hash({"withdraws_publish": prev["publish_no"], "reason": reason, "at": now})
        pub_no = "PUB-" + _seq(self.conn, "publishes", "id")
        cur = self.conn.execute(
            """INSERT INTO publishes(publish_no, proposal_id, revision_no, kind,
                                     source_standard_id, target_standard_id, mapping_hash,
                                     prev_publish_id, published_by, released_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (pub_no, p["id"], p["revision_no"], PublishKind.WITHDRAWAL.value,
             p["source_standard_id"], p["target_standard_id"], digest,
             prev["id"], actor, now),
        )
        pub_id = int(cur.lastrowid)
        self.conn.execute("UPDATE publishes SET superseded_at=? WHERE id=?", (now, prev["id"]))
        self.conn.execute(
            "UPDATE proposals SET status=?, current_publish_id=? WHERE id=?",
            (ProposalStatus.WITHDRAWN.value, pub_id, p["id"]))
        self._event(p["id"], "PUBLISHED_WITHDRAWAL", actor,
                    {"publish_id": pub_id, "reason": reason})
        return pub_id

    # ── 标准换版后的更新提案 ───────────────────────────────────────────

    def propose_update(self, prev_publish_id: int, creator: str, *,
                       new_source_standard_id: int | None = None,
                       new_target_standard_id: int | None = None,
                       title: str | None = None) -> dict:
        """基于历史发布版本，针对换版后的标准起草新提案并按编码继承映射项。"""
        prev = self.conn.execute("SELECT * FROM publishes WHERE id=?", (prev_publish_id,)).fetchone()
        if prev is None:
            raise NotFoundError(f"发布版本不存在：{prev_publish_id}")
        if prev["kind"] != PublishKind.MAPPING.value:
            raise WorkflowError("只能基于映射发布版本发起更新")
        new_src_id = new_source_standard_id or prev["source_standard_id"]
        new_tgt_id = new_target_standard_id or prev["target_standard_id"]
        if new_src_id == prev["source_standard_id"] and new_tgt_id == prev["target_standard_id"]:
            raise DomainValidationError("更新提案至少有一侧标准完成换版")
        src, tgt = self._effective(new_src_id), self._effective(new_tgt_id)

        no = "MAP-" + _seq(self.conn, "proposals", "id")
        cur = self.conn.execute(
            """INSERT INTO proposals(proposal_no, title, source_standard_id, target_standard_id,
                                     status, creator, based_on_publish_id, revision_no, created_at)
               VALUES (?,?,?,?,?,?,?,1,?)""",
            (no, title or f"换版更新（基于 {prev['publish_no']}）", new_src_id, new_tgt_id,
             ProposalStatus.DRAFT.value, creator, prev_publish_id, _now()),
        )
        pid = int(cur.lastrowid)

        # 按编码继承：单元与两侧证据编码仍存在才保留，其余保守丢弃交由专家重判
        old_lines = self.conn.execute("SELECT * FROM publish_lines WHERE publish_id=? ORDER BY ordinal",
                                      (prev_publish_id,)).fetchall()
        src_units, tgt_units = self._unit_map(new_src_id), self._unit_map(new_tgt_id)
        src_codes = {r["id"]: r["code"] for r in self.conn.execute(
            "SELECT id, code FROM units WHERE standard_id=?", (new_src_id,))}
        tgt_codes = {r["id"]: r["code"] for r in self.conn.execute(
            "SELECT id, code FROM units WHERE standard_id=?", (new_tgt_id,))}
        old_src_codes = {r["id"]: r["code"] for r in self.conn.execute(
            "SELECT id, code FROM units WHERE standard_id=?", (prev["source_standard_id"],))}
        old_tgt_codes = {r["id"]: r["code"] for r in self.conn.execute(
            "SELECT id, code FROM units WHERE standard_id=?", (prev["target_standard_id"],))}
        seeded, dropped_gaps = 0, 0
        for line in old_lines:
            old_s = old_src_codes.get(line["source_unit_id"])
            old_t = old_tgt_codes.get(line["target_unit_id"]) if line["target_unit_id"] else None
            if old_s not in src_units:
                continue
            if line["decision"] != Decision.NONE.value and old_t not in tgt_units:
                continue
            gaps = []
            for g in self.conn.execute("SELECT * FROM publish_line_gaps WHERE publish_line_id=?",
                                       (line["id"],)).fetchall():
                # “目标缺少证据”是对旧版图谱的事实断言；新版证据集已变，必须重判，保守丢弃
                if g["gap_kind"] == GapKind.EVIDENCE_MISSING.value:
                    dropped_gaps += 1
                    continue
                se_code = self._evidence_code(g["source_evidence_id"]) if g["source_evidence_id"] else None
                te_code = self._evidence_code(g["target_evidence_id"]) if g["target_evidence_id"] else None
                se_ok = se_code is None or se_code in self._evidence_map(src_units[old_s])
                te_ok = te_code is None or (old_t in tgt_units and te_code in self._evidence_map(tgt_units[old_t]))
                if se_ok and te_ok:
                    gaps.append({"gap_kind": g["gap_kind"], "source_evidence_code": se_code,
                                 "target_evidence_code": te_code, "description": g["description"]})
                else:
                    dropped_gaps += 1
            self._insert_line(pid, 1, src_units[old_s],
                              tgt_units.get(old_t) if old_t else None,
                              line["decision"], line["rationale"], gaps, seeded)
            seeded += 1
        digest = content_hash(self._snapshot_payload(
            src, tgt, self._load_items(pid, 1))) if seeded else None
        self.conn.execute(
            """INSERT INTO proposal_revisions(proposal_id, revision_no, snapshot_hash, change_note,
                                              created_by, created_at)
               VALUES (?,1,?,?,?,?)""",
            (pid, digest, f"继承自 {prev['publish_no']} 的换版更新草案", creator, _now()),
        )
        self.conn.execute("UPDATE proposals SET snapshot_hash=? WHERE id=?", (digest, pid))
        self._event(pid, "CREATED", creator,
                    {"proposal_no": no, "based_on_publish": prev["publish_no"],
                     "seeded_lines": seeded, "dropped_gaps": dropped_gaps})
        self.conn.commit()
        return {"proposal_id": pid, "proposal_no": no, "seeded_lines": seeded,
                "dropped_gaps": dropped_gaps}

    def _evidence_code(self, evidence_id: int) -> str | None:
        r = self.conn.execute("SELECT code FROM evidence_requirements WHERE id=?", (evidence_id,)).fetchone()
        return r["code"] if r else None

    # ── 读写映射项 ─────────────────────────────────────────────────────

    def _insert_line(self, proposal_id: int, revision_no: int, source_unit_id: int,
                     target_unit_id: int | None, decision: str, rationale: str,
                     gaps: list[dict], ordinal: int) -> int:
        cur = self.conn.execute(
            """INSERT INTO line_items(proposal_id, revision_no, source_unit_id, target_unit_id,
                                      decision, rationale, ordinal)
               VALUES (?,?,?,?,?,?,?)""",
            (proposal_id, revision_no, source_unit_id, target_unit_id, decision, rationale, ordinal),
        )
        line_id = int(cur.lastrowid)
        src_ev = self._evidence_map(source_unit_id)
        tgt_ev = self._evidence_map(target_unit_id) if target_unit_id else {}
        for j, g in enumerate(gaps):
            self.conn.execute(
                """INSERT INTO item_gaps(line_item_id, source_evidence_id, target_evidence_id,
                                         gap_kind, description, ordinal)
                   VALUES (?,?,?,?,?,?)""",
                (line_id,
                 src_ev.get(g["source_evidence_code"]) if g.get("source_evidence_code") else None,
                 tgt_ev.get(g["target_evidence_code"]) if g.get("target_evidence_code") else None,
                 g["gap_kind"], g["description"], j),
            )
        return line_id

    def _write_items(self, proposal_id: int, revision_no: int, norm: list[dict],
                     src_units: dict[str, int], tgt_units: dict[str, int]) -> None:
        for ordinal, item in enumerate(norm):
            self._insert_line(proposal_id, revision_no,
                              src_units[item["source_unit_code"]],
                              tgt_units.get(item["target_unit_code"]) if item["target_unit_code"] else None,
                              item["decision"], item["rationale"], item["gaps"], ordinal)

    def _load_items(self, proposal_id: int, revision_no: int) -> list[dict]:
        src_codes = {r["id"]: r["code"] for r in self.conn.execute(
            """SELECT u.id, u.code FROM units u
               JOIN proposals pr ON pr.source_standard_id=u.standard_id
               WHERE pr.id=?""", (proposal_id,))}
        tgt_codes = {r["id"]: r["code"] for r in self.conn.execute(
            """SELECT u.id, u.code FROM units u
               JOIN proposals pr ON pr.target_standard_id=u.standard_id
               WHERE pr.id=?""", (proposal_id,))}
        lines = self.conn.execute(
            "SELECT * FROM line_items WHERE proposal_id=? AND revision_no=? ORDER BY ordinal",
            (proposal_id, revision_no)).fetchall()
        result = []
        for line in lines:
            gaps = []
            for g in self.conn.execute("SELECT * FROM item_gaps WHERE line_item_id=? ORDER BY ordinal",
                                       (line["id"],)).fetchall():
                gaps.append({
                    "gap_kind": g["gap_kind"],
                    "source_evidence_code": self._ev_code_of(g["source_evidence_id"]),
                    "target_evidence_code": self._ev_code_of(g["target_evidence_id"]),
                    "description": g["description"],
                })
            result.append({
                "source_unit_code": src_codes[line["source_unit_id"]],
                "target_unit_code": tgt_codes.get(line["target_unit_id"]),
                "decision": line["decision"],
                "rationale": line["rationale"],
                "gaps": gaps,
            })
        return result

    def _ev_code_of(self, evidence_id: int | None) -> str | None:
        if evidence_id is None:
            return None
        r = self.conn.execute("SELECT code FROM evidence_requirements WHERE id=?", (evidence_id,)).fetchone()
        return r["code"] if r else None

    # ── 查询 ───────────────────────────────────────────────────────────

    def get_proposal(self, proposal_id: int) -> dict:
        p = self._require(proposal_id)
        src = self.standards.get_standard(p["source_standard_id"])
        tgt = self.standards.get_standard(p["target_standard_id"])
        signoffs = [dict(r) for r in self.conn.execute(
            "SELECT party, signer, signed_at FROM signoffs WHERE proposal_id=? AND revision_no=? ORDER BY party",
            (proposal_id, p["revision_no"]))]
        revisions = [dict(r) for r in self.conn.execute(
            "SELECT revision_no, snapshot_hash, change_note, created_by, created_at FROM proposal_revisions "
            "WHERE proposal_id=? ORDER BY revision_no", (proposal_id,))]
        events = [dict(r) for r in self.conn.execute(
            "SELECT seq, event_type, actor, payload_json, at FROM proposal_events "
            "WHERE proposal_id=? ORDER BY seq", (proposal_id,))]
        return {
            "id": p["id"],
            "proposal_no": p["proposal_no"],
            "title": p["title"],
            "status": p["status"],
            "creator": p["creator"],
            "revision_no": p["revision_no"],
            "decision_seq": p["decision_seq"],
            "snapshot_hash": p["snapshot_hash"],
            "based_on_publish_id": p["based_on_publish_id"],
            "current_publish_id": p["current_publish_id"],
            "submitted_at": p["submitted_at"],
            "created_at": p["created_at"],
            "source_standard": {"id": src["id"], "code": src["code"], "version_no": src["version_no"],
                                "content_hash": src["content_hash"]},
            "target_standard": {"id": tgt["id"], "code": tgt["code"], "version_no": tgt["version_no"],
                                "content_hash": tgt["content_hash"]},
            "signoffs": signoffs,
            "revisions": revisions,
            "items": self._load_items(proposal_id, p["revision_no"]),
            "events": events,
        }

    def list_proposals(self) -> list[dict]:
        rows = self.conn.execute("SELECT id FROM proposals ORDER BY id").fetchall()
        return [self.get_proposal(r["id"]) for r in rows]
