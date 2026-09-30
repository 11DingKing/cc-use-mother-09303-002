"""HTTP API（标准库 http.server，零第三方依赖）。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .app import SkillMapApp
from .domain.errors import (
    ConcurrentSignoffError,
    DomainValidationError,
    NotFoundError,
    SigningConditionError,
    SkillMapError,
    WorkflowError,
)


def make_handler(app: SkillMapApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SkillMap/1.0"

        def log_message(self, fmt: str, *args) -> None:  # 安静：错误另走响应体
            pass

        # ── 基础 ───────────────────────────────────────────────────────

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise DomainValidationError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(value, dict):
                raise DomainValidationError("请求体必须是 JSON 对象")
            return value

        def _guard(self, fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ConcurrentSignoffError as exc:
                self._json(409, {"error_type": "concurrent_signoff", "message": str(exc)})
            except SigningConditionError as exc:
                self._json(409, {"error_type": "signing_condition", "message": str(exc)})
            except WorkflowError as exc:
                self._json(409, {"error_type": "workflow", "message": str(exc)})
            except DomainValidationError as exc:
                self._json(422, {"error_type": "validation", "errors": exc.errors})
            except NotFoundError as exc:
                self._json(404, {"error_type": "not_found", "message": str(exc)})
            except SkillMapError as exc:
                self._json(400, {"error_type": "domain", "message": str(exc)})

        def _q(self, qs: dict, name: str, required: bool = True, cast=int):
            raw = qs.get(name, [None])[0]
            if raw is None:
                if required:
                    raise DomainValidationError(f"缺少查询参数：{name}")
                return None
            try:
                return cast(raw)
            except (TypeError, ValueError) as exc:
                raise DomainValidationError(f"查询参数 {name} 非法：{raw}") from exc

        # ── 路由 ───────────────────────────────────────────────────────

        def do_GET(self) -> None:
            with app.write_lock:
                self._guard(self._route_get)

        def do_POST(self) -> None:
            with app.write_lock:
                self._guard(self._route_post)

        def _route_get(self) -> None:
            url = urlparse(self.path)
            path, qs = url.path, parse_qs(url.query)

            if path == "/api/standards":
                self._json(200, app.standards.list_standards(self._q(qs, "code", False, str)))
            elif m := re.fullmatch(r"/api/standards/(\d+)", path):
                self._json(200, app.standards.get_standard(int(m.group(1))))
            elif path == "/api/proposals":
                self._json(200, app.proposals.list_proposals())
            elif m := re.fullmatch(r"/api/proposals/(\d+)", path):
                self._json(200, app.proposals.get_proposal(int(m.group(1))))
            elif m := re.fullmatch(r"/api/publishes/(\d+)", path):
                self._json(200, app.publishes.get_publish(int(m.group(1))))
            elif path == "/api/history":
                self._json(200, app.publishes.history(
                    self._q(qs, "source"), self._q(qs, "target")))
            elif path == "/api/current":
                self._json(200, app.publishes.current(
                    self._q(qs, "source"), self._q(qs, "target")))
            elif path == "/api/certificates":
                self._json(200, app.publishes.list_certificates())
            elif m := re.fullmatch(r"/api/certificates/([^/]+)", path):
                self._json(200, app.publishes.get_certificate(m.group(1)))
            elif m := re.fullmatch(r"/api/publishes/(\d+)/recompute", path):
                self._json(200, app.comparison.recompute_publish_hash(int(m.group(1))))
            elif path == "/api/compare":
                self._json(200, app.comparison.compare(self._q(qs, "a"), self._q(qs, "b")))
            elif path == "/api/health":
                self._json(200, {"status": "ok"})
            else:
                self._json(404, {"message": "未知路由"})

        def _route_post(self) -> None:
            url = urlparse(self.path)
            path, body = url.path, self._body()

            if path == "/api/jurisdictions":
                sid = app.standards.create_jurisdiction(body["code"], body["name"])
                self._json(201, {"id": sid})
            elif path == "/api/standards":
                sid = app.standards.import_standard(body["jurisdiction_code"], body)
                self._json(201, {"id": sid})
            elif m := re.fullmatch(r"/api/standards/(\d+)/effective", path):
                app.standards.mark_effective(int(m.group(1)), body.get("effective_at"))
                self._json(200, {"id": int(m.group(1)), "status": "EFFECTIVE"})
            elif m := re.fullmatch(r"/api/standards/(\d+)/new-version", path):
                sid = app.standards.new_version_draft(int(m.group(1)), body)
                self._json(201, {"id": sid})
            elif path == "/api/proposals":
                pid = app.proposals.create_proposal(
                    body["title"], body["source_standard_id"], body["target_standard_id"],
                    body["creator"], body["items"])
                self._json(201, {"id": pid})
            elif m := re.fullmatch(r"/api/proposals/(\d+)/revisions", path):
                rev = app.proposals.revise(int(m.group(1)), body["items"], body["actor"],
                                           body.get("change_note", ""))
                self._json(201, {"revision_no": rev})
            elif m := re.fullmatch(r"/api/proposals/(\d+)/submit", path):
                app.proposals.submit_for_review(int(m.group(1)), body["actor"])
                self._json(200, {"id": int(m.group(1)), "status": "IN_REVIEW"})
            elif m := re.fullmatch(r"/api/proposals/(\d+)/request-changes", path):
                app.proposals.request_changes(int(m.group(1)), body["actor"], body.get("note", ""))
                self._json(200, {"id": int(m.group(1)), "status": "DRAFT"})
            elif m := re.fullmatch(r"/api/proposals/(\d+)/signoffs", path):
                result = app.proposals.signoff(
                    int(m.group(1)), body["party"], body["signer"], body["expected_revision"])
                self._json(200 if not result["publish_id"] else 201, result)
            elif m := re.fullmatch(r"/api/proposals/(\d+)/withdraw", path):
                self._json(200, app.proposals.withdraw(
                    int(m.group(1)), body["actor"], body.get("reason", "")))
            elif path == "/api/updates":
                self._json(201, app.proposals.propose_update(
                    body["based_on_publish_id"], body["creator"],
                    new_source_standard_id=body.get("new_source_standard_id"),
                    new_target_standard_id=body.get("new_target_standard_id"),
                    title=body.get("title")))
            elif path == "/api/certificates":
                cid = app.publishes.issue_certificate(
                    body["certificate_no"], body["holder"], body["source_standard_id"],
                    body["grade_label"], body["publish_id"], body["issuer"])
                self._json(201, {"id": cid})
            else:
                self._json(404, {"message": "未知路由"})

    return Handler
