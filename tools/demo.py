"""焊接技能互认端到端演示：

院校甲（CN 管辖区）与院校乙（DE 管辖区）互认焊接等级证书。
覆盖：标准版本导入/冻结、逐项映射（含部分互认与证据差距）、双方会签发布、
历史证书钉版、标准换版后的更新提案、任意两版比较与复算、发布后撤回。

运行：PYTHONPATH=src python3 tools/demo.py
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skillmap.app import SkillMapApp
from skillmap.domain.errors import ConcurrentSignoffError


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cn_welding_v1() -> dict:
    return {
        "code": "WELD-CN", "title": "焊接职业技能标准（中国）", "version_no": "2024",
        "publisher": "中国焊接标准工作组",
        "units": [
            {"code": "CN-SMAW-PLATE", "title": "焊条电弧焊-板对接", "level_label": "中级 4 级",
             "scope": "碳钢平板对接，位置 1G/2G，板厚 8-20mm",
             "evidence": [
                 {"code": "E-TH", "title": "理论考试", "evidence_kind": "THEORY", "requirement": "焊接工艺与安全笔试≥80分"},
                 {"code": "E-PR", "title": "实操试件", "evidence_kind": "PRACTICAL", "requirement": "1G/2G 位置试件，外观+RT 探伤合格"},
             ], "requires": []},
            {"code": "CN-SMAW-PIPE", "title": "焊条电弧焊-管对接", "level_label": "高级 5 级",
             "scope": "碳钢管道对接，位置 5G/6G，管径≥150mm",
             "evidence": [
                 {"code": "E-TH", "title": "理论考试", "evidence_kind": "THEORY", "requirement": "管道焊接工艺笔试≥80分"},
                 {"code": "E-PR", "title": "实操试件", "evidence_kind": "PRACTICAL", "requirement": "6G 位置管试件，RT 探伤合格"},
                 {"code": "E-OBS", "title": "现场作业观察", "evidence_kind": "OBSERVATION", "requirement": "施工现场连续观察记录 8 学时"},
             ], "requires": ["CN-SMAW-PLATE"]},
        ],
    }


def de_welding_v1() -> dict:
    return {
        "code": "WELD-DE", "title": "Schweißerqualifikation（德国合作版）", "version_no": "2023",
        "publisher": "合作院校乙标准委员会",
        "units": [
            {"code": "DE-BLECH-E", "title": "Lichtbogenhandschweißen Blech", "level_label": "Stufe II",
             "scope": "Stahlblech, Positionen PA/PB, 8-20mm",
             "evidence": [
                 {"code": "THEO", "title": "Theorieprüfung", "evidence_kind": "THEORY", "requirement": "Schriftliche Prüfung ≥ 80%"},
                 {"code": "PRAX", "title": "Praxisprüfung Werkstück", "evidence_kind": "PRACTICAL", "requirement": "Werkstück PA/PB, Sicht+RT bestanden"},
             ], "requires": []},
            {"code": "DE-ROHR-R", "title": "Lichtbogenhandschweißen Rohr", "level_label": "Stufe III",
             "scope": "Stahlrohr, Positionen PF/H-L045, ≥150mm",
             "evidence": [
                 {"code": "THEO", "title": "Theorieprüfung", "evidence_kind": "THEORY", "requirement": "Rohrschweißen Theorie ≥ 80%"},
                 {"code": "PRAX", "title": "Praxisprüfung Rohr", "evidence_kind": "PRACTICAL", "requirement": "Rohr H-L045, RT bestanden"},
             ], "requires": ["DE-BLECH-E"]},
        ],
    }


def de_welding_v2() -> dict:
    """换版：管道单元增加施工现场观察证据（新版更严）。"""
    v2 = de_welding_v1()
    v2["version_no"] = "2026"
    v2["units"][1]["evidence"].append(
        {"code": "BAUST", "title": "Baustellenbeobachtung", "evidence_kind": "OBSERVATION",
         "requirement": "Baustellenbeobachtung 8 UE"})
    return v2


def main() -> None:
    app = SkillMapApp.open(":memory:")

    # 1. 导入两个管辖区与生效标准
    app.standards.create_jurisdiction("CN", "中国合作院校甲")
    app.standards.create_jurisdiction("DE", "德国合作院校乙")
    cn = app.standards.import_standard("CN", cn_welding_v1())
    app.standards.mark_effective(cn)
    de = app.standards.import_standard("DE", de_welding_v1())
    app.standards.mark_effective(de)
    show("两国标准版本生效（能力图谱已冻结）", {
        "CN": {"id": cn, "content_hash": app.standards.get_standard(cn)["content_hash"]},
        "DE": {"id": de, "content_hash": app.standards.get_standard(de)["content_hash"]},
    })

    # 2. 逐项映射提案：板=完全互认；管=部分互认（DE 缺现场观察证据、位置不等同）
    items_v1 = [
        {"source_unit_code": "CN-SMAW-PLATE", "target_unit_code": "DE-BLECH-E", "decision": "FULL",
         "rationale": "同为碳钢平板 1G/2G↔PA/PB、8-20mm，理论与实操证据要求等同", "gaps": []},
        {"source_unit_code": "CN-SMAW-PIPE", "target_unit_code": "DE-ROHR-R", "decision": "PARTIAL",
         "rationale": "管道 6G 覆盖 H-L045，但 DE 课程无现场观察证据要求",
         "gaps": [
             {"gap_kind": "EVIDENCE_MISSING", "source_evidence_code": "E-OBS",
              "target_evidence_code": None, "description": "DE Stufe III 不要求施工现场连续观察 8 学时"},
             {"gap_kind": "SCOPE_NARROWER", "source_evidence_code": "E-PR",
              "target_evidence_code": "PRAX",
              "description": "6G（45°固定管）范围大于 H-L045，逆向免修时需补位位置训练"},
         ]},
    ]
    pid = app.proposals.create_proposal("中德焊接等级互认（第一版）", cn, de, "标准专家-李", items_v1)
    app.proposals.submit_for_review(pid, "标准专家-李")
    show("提案提交会审", {"proposal_id": pid, "snapshot_hash": app.proposals.get_proposal(pid)["snapshot_hash"]})

    # 3. 并发会签：两个线程同时提交，第二个重复签署得到 409 类冲突而非覆盖
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def sign(party: str, signer: str) -> None:
        barrier.wait()
        try:
            results.append(app.proposals.signoff(pid, party, signer, expected_revision=1))
        except ConcurrentSignoffError as exc:
            results.append({"party": party, "conflict": str(exc)})

    t1 = threading.Thread(target=sign, args=("A", "甲方专家组-王"))
    t2 = threading.Thread(target=sign, args=("B", "乙方专家组-Müller"))
    t1.start(); t2.start(); t1.join(); t2.join()
    show("双方并发会签（一方触发原子发布）", results)
    pub1 = app.proposals.get_proposal(pid)["current_publish_id"]

    # 4. 依据当时映射签发历史证书（钉版）
    cert = app.publishes.issue_certificate("CERT-2026-0001", "学员-张伟", cn, "中级 4 级", pub1, "认证机构-甲")
    show("证书签发（钉住发布版本与摘要）", app.publishes.get_certificate(cert))

    # 5. DE 标准换版 2023→2026：旧版被替代，基于旧发布发起更新提案
    de2 = app.standards.new_version_draft(de, de_welding_v2())
    app.standards.mark_effective(de2)
    upd = app.proposals.propose_update(pub1, "标准专家-李", new_target_standard_id=de2)
    show("标准换版后按编码继承生成更新草案",
         {"proposal_id": upd["proposal_id"], "seeded_lines": upd["seeded_lines"],
          "dropped_gaps": upd["dropped_gaps"],
          "note": "新增 BAUST 证据后，原 EVIDENCE_MISSING 差距项被保守丢弃，需专家重判"})
    # 专家重新判定：新版补齐现场观察 → 管道差距收窄为仅位置差异，仍为部分互认
    p2 = upd["proposal_id"]
    app.proposals.revise(p2, [
        {"source_unit_code": "CN-SMAW-PLATE", "target_unit_code": "DE-BLECH-E", "decision": "FULL",
         "rationale": "板对接证据与范围仍等同（2026 版未调整）", "gaps": []},
        {"source_unit_code": "CN-SMAW-PIPE", "target_unit_code": "DE-ROHR-R", "decision": "PARTIAL",
         "rationale": "2026 版新增 Baustellenbeobachtung 与 E-OBS 等同；仍存在位置范围差距",
         "gaps": [
             {"gap_kind": "SCOPE_NARROWER", "source_evidence_code": "E-PR",
              "target_evidence_code": "PRAX",
              "description": "6G 与 H-L045 位置范围差异仍在，逆向免修需补位训练"},
         ]},
    ], "标准专家-李", "依据 DE 2026 新版重判管道差距")
    app.proposals.submit_for_review(p2, "标准专家-李")
    app.proposals.signoff(p2, "A", "甲方专家组-王", 2)
    app.proposals.signoff(p2, "B", "乙方专家组-Müller", 2)
    pub2 = app.proposals.get_proposal(p2)["current_publish_id"]

    # 6. 任意两版比较与复算
    cmp_result = app.comparison.compare(pub1, pub2)
    show(f"两版映射比较 {cmp_result['a']['publish_no']} → {cmp_result['b']['publish_no']}",
         {"summary": cmp_result["summary"], "changed": cmp_result["changed"]})
    show("发布版本复算（从冻结行重建并比对摘要）", {
        "pub1": {k: v for k, v in app.comparison.recompute_publish_hash(pub1).items() if k != "payload"},
        "pub2": {k: v for k, v in app.comparison.recompute_publish_hash(pub2).items() if k != "payload"},
    })

    # 7. 历史证书始终指向当时的映射
    cert_view = app.publishes.get_certificate(cert)
    show("换版后历史证书仍钉在旧版", {
        "certificate_no": cert_view["certificate_no"],
        "adopted_publish": cert_view["adopted_publish"],
        "current_mapping_changed": cert_view["current_mapping_changed"],
    })

    # 8. 发布后撤回互认 → 新版本（WITHDRAWAL），历史不删除
    withdrawal = app.proposals.withdraw(p2, "标准工作组", "合作协议到期终止互认")
    show("已发布映射撤回（生成撤销版本，历史保留）", {
        "withdrawal": withdrawal,
        "history": [{"publish_no": p["publish_no"], "kind": p["kind"]}
                    for p in app.publishes.history(cn, de) + app.publishes.history(cn, de2)],
        "certificate_still_resolvable": app.publishes.get_certificate(cert)["adopted_publish"],
    })


if __name__ == "__main__":
    main()
