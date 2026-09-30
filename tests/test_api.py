"""HTTP API 端到端集成测试（标准库 http.client 回环）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from skillmap.api import make_handler
from skillmap.app import SkillMapApp
from tests._helpers import welding_standard


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = SkillMapApp.open(":memory:")
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"} if body is not None else {})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def _seed_standards(self) -> tuple[int, int]:
        self.call("POST", "/api/jurisdictions", {"code": "CN", "name": "甲方"})
        self.call("POST", "/api/jurisdictions", {"code": "DE", "name": "乙方"})
        _, s = self.call("POST", "/api/standards", {"jurisdiction_code": "CN", **welding_standard("CN")})
        cn = s["id"]
        self.call("POST", f"/api/standards/{cn}/effective", {})
        _, s = self.call("POST", "/api/standards",
                         {"jurisdiction_code": "DE",
                          **welding_standard("DE", version="2023", extra_pipe_evidence=True)})
        de = s["id"]
        self.call("POST", f"/api/standards/{de}/effective", {})
        return cn, de

    def test_health_and_full_flow(self) -> None:
        status, body = self.call("GET", "/api/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

        cn, de = self._seed_standards()
        items = [
            {"source_unit_code": "CN-PLATE", "target_unit_code": "DE-PLATE", "decision": "FULL",
             "rationale": "板等同", "gaps": []},
            {"source_unit_code": "CN-PIPE", "target_unit_code": "DE-PIPE", "decision": "PARTIAL",
             "rationale": "位置差异",
             "gaps": [{"gap_kind": "SCOPE_NARROWER", "source_evidence_code": "PR",
                       "target_evidence_code": "PR", "description": "6G 位置更宽"}]},
        ]
        status, body = self.call("POST", "/api/proposals",
                                 {"title": "互认", "source_standard_id": cn,
                                  "target_standard_id": de, "creator": "甲", "items": items})
        self.assertEqual(status, 201)
        pid = body["id"]

        status, body = self.call("POST", f"/api/proposals/{pid}/submit", {"actor": "甲"})
        self.assertEqual(status, 200)
        status, body = self.call("POST", f"/api/proposals/{pid}/signoffs",
                                 {"party": "A", "signer": "王", "expected_revision": 1})
        self.assertEqual(status, 200)
        self.assertIsNone(body["publish_id"])
        status, body = self.call("POST", f"/api/proposals/{pid}/signoffs",
                                 {"party": "B", "signer": "Müller", "expected_revision": 1})
        self.assertEqual(status, 201)
        pub = body["publish_id"]

        # 发布后再签 → 409 workflow（签署通道已关闭；重复签署的并发冲突由领域测试覆盖）
        status, body = self.call("POST", f"/api/proposals/{pid}/signoffs",
                                 {"party": "A", "signer": "王", "expected_revision": 1})
        self.assertEqual(status, 409)
        self.assertEqual(body["error_type"], "workflow")

        # 历史证书
        status, body = self.call("POST", "/api/certificates",
                                 {"certificate_no": "C-1", "holder": "张伟",
                                  "source_standard_id": cn, "grade_label": "中级",
                                  "publish_id": pub, "issuer": "机构"})
        self.assertEqual(status, 201)
        status, cert = self.call("GET", "/api/certificates/C-1")
        self.assertEqual(cert["adopted_publish"]["id"], pub)

        # 复算与比较
        status, rec = self.call("GET", f"/api/publishes/{pub}/recompute")
        self.assertTrue(rec["matches"])
        status, hist = self.call("GET", f"/api/history?source={cn}&target={de}")
        self.assertEqual(len(hist), 1)

    def test_validation_error_shape(self) -> None:
        self._seed_standards()
        status, body = self.call("POST", "/api/standards",
                                 {"jurisdiction_code": "CN", "code": "X", "units": []})
        self.assertEqual(status, 422)
        self.assertEqual(body["error_type"], "validation")
        self.assertIsInstance(body["errors"], list)

    def test_not_found(self) -> None:
        status, body = self.call("GET", "/api/standards/999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error_type"], "not_found")

    def test_withdraw_published_flow(self) -> None:
        cn, de = self._seed_standards()
        items = [
            {"source_unit_code": "CN-PLATE", "target_unit_code": "DE-PLATE", "decision": "FULL",
             "rationale": "板等同", "gaps": []},
            {"source_unit_code": "CN-PIPE", "target_unit_code": "DE-PIPE", "decision": "NONE",
             "rationale": "管道不互认", "gaps": []},
        ]
        _, body = self.call("POST", "/api/proposals",
                            {"title": "p", "source_standard_id": cn, "target_standard_id": de,
                             "creator": "甲", "items": items})
        pid = body["id"]
        self.call("POST", f"/api/proposals/{pid}/submit", {"actor": "甲"})
        self.call("POST", f"/api/proposals/{pid}/signoffs",
                  {"party": "A", "signer": "王", "expected_revision": 1})
        status, body = self.call("POST", f"/api/proposals/{pid}/signoffs",
                                 {"party": "B", "signer": "Müller", "expected_revision": 1})
        pub = body["publish_id"]
        status, body = self.call("POST", f"/api/proposals/{pid}/withdraw",
                                 {"actor": "组", "reason": "终止"})
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "WITHDRAWAL")
        status, cur = self.call("GET", f"/api/current?source={cn}&target={de}")
        self.assertEqual(cur["kind"], "WITHDRAWAL")
        # 历史映射版本仍可读
        status, old = self.call("GET", f"/api/publishes/{pub}")
        self.assertEqual(len(old["lines"]), 2)


if __name__ == "__main__":
    unittest.main()
