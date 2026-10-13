#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api-batch-tester 自检：起一个本地 mock API，端到端验证引擎真的在发请求、真的在断言。

    python self_test.py

覆盖三个关键结论（这三条必须成立，否则技能就是"空壳"）：
    1. 引擎确实发出了 HTTP 请求（mock 服务端能记录到访问）
    2. 正常接口判定为通过、故意有问题的接口判定为失败
    3. 报告与用例集是真实数据，不含任何 "[待填充]" 占位符
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import api_batch_tester_tool as engine  # noqa: E402

HITS: list[str] = []
LOCK = threading.Lock()


class MockAPI(BaseHTTPRequestHandler):
    server_version = "MockAPI/1.0"

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        with LOCK:
            HITS.append("GET " + self.path)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/users":
            return self._json(200, [{"id": 1, "name": "alice"}])

        m = re.match(r"^/users/([^/]+)$", path)
        if m:
            uid = m.group(1)
            if not uid.isdigit():
                return self._json(400, {"error": "invalid id"})
            if uid == "999":
                return self._json(404, {"error": "not found"})
            return self._json(200, {"id": int(uid), "name": "alice", "email": "alice@example.com"})

        if path == "/search":
            # 故意不校验必填参数 q —— 用来验证引擎能否发现"接口缺少输入校验"
            total = len(query.get("q", [""])[0])
            return self._json(200, {"items": [], "total": total})

        if path == "/slow":
            time.sleep(2.2)  # 故意慢，用来验证慢接口识别
            return self._json(200, {"ok": True})

        if path == "/broken":
            return self._json(500, {"error": "internal server error"})  # 故意 500

        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        with LOCK:
            HITS.append("POST " + self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return self._json(400, {"error": "body is not valid json"})
        if not isinstance(data, dict) or "name" not in data:
            return self._json(422, {"error": "name is required"})
        return self._json(201, {"id": 2, "name": data.get("name", "")})

    def log_message(self, fmt, *args):  # noqa: A002
        return


def build_spec(base_url: str) -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "api-batch-tester Self Test API", "version": "1.0.0"},
        "servers": [{"url": base_url}],
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "summary": "用户列表",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": ["id", "name"],
                                            "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                                        },
                                    }
                                }
                            },
                        }
                    },
                },
                "post": {
                    "operationId": "createUser",
                    "summary": "创建用户",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id", "name"],
                                        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                },
            },
            "/users/{id}": {
                "get": {
                    "operationId": "getUser",
                    "summary": "用户详情",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id", "name"],
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "name": {"type": "string"},
                                            "email": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/search": {
                "get": {
                    "operationId": "search",
                    "summary": "搜索",
                    "parameters": [
                        {"name": "q", "in": "query", "required": True, "schema": {"type": "string", "maxLength": 20}}
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["items", "total"],
                                        "properties": {"items": {"type": "array"}, "total": {"type": "integer"}},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/slow": {
                "get": {
                    "operationId": "slowEndpoint",
                    "summary": "慢接口",
                    "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/broken": {
                "get": {
                    "operationId": "brokenEndpoint",
                    "summary": "故意返回 500",
                    "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
        },
    }


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[self-test] mock API 已启动：{base_url}")

    spec_path = os.path.join(engine.DATA_DIR, "_selftest_openapi.json")
    os.makedirs(engine.DATA_DIR, exist_ok=True)
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(build_spec(base_url), fh, ensure_ascii=False, indent=2)

    report_path = os.path.join(engine.DATA_DIR, "_selftest_report.md")
    cases_path = os.path.join(engine.DATA_DIR, "_selftest_cases.json")

    print("[self-test] 调用引擎：run --concurrency 6 ...")
    code = engine.main(
        [
            "run",
            "--spec",
            spec_path,
            "--concurrency",
            "6",
            "--timeout",
            "8",
            "--report",
            report_path,
            "--cases-out",
            cases_path,
        ]
    )
    time.sleep(0.2)
    server.shutdown()

    print("\n" + "=" * 60)
    print("自检结果")
    print("=" * 60)

    checks: list[tuple[str, bool, str]] = []

    # 1) 引擎真的发了请求
    with LOCK:
        hit_count = len(HITS)
    checks.append(("引擎发出了真实 HTTP 请求", hit_count >= 6, f"mock 服务端记录到 {hit_count} 次访问"))

    # 2) 产物存在
    report_text = ""
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as fh:
            report_text = fh.read()
    checks.append(("生成 Markdown 报告", len(report_text) > 800, f"{len(report_text)} 字符"))

    data = {}
    if os.path.exists(cases_path):
        with open(cases_path, encoding="utf-8") as fh:
            data = json.load(fh)
    checks.append(("生成用例集 JSON", bool(data.get("results")), f"{len(data.get('results') or [])} 条用例结果"))

    # 3) 报告不是占位符
    no_placeholder = "[待填充]" not in report_text
    checks.append(("报告不含占位符", no_placeholder, "未发现 [待填充]" if no_placeholder else "仍含占位符"))

    # 4) 断言真的生效：正常接口通过、故意的问题接口失败
    results = data.get("results") or []
    by_id = {r["id"]: r for r in results}
    ok_normal = any(r["passed"] and r["endpoint"] == "GET /users" for r in results)
    checks.append(("正常接口判定为通过", ok_normal, "GET /users 通过"))

    broken = by_id.get("GET-/broken#normal")
    checks.append(
        (
            "故意 500 的接口判定为失败",
            bool(broken) and not broken["passed"] and broken["status"] == 500,
            f"GET /broken 实际 {broken['status'] if broken else '?'} → 判定失败",
        )
    )

    missing_q = by_id.get("GET-/search#missing-q")
    checks.append(
        (
            "发现接口未校验必填参数",
            bool(missing_q) and not missing_q["passed"],
            f"GET /search 缺 q 时返回 {missing_q['status'] if missing_q else '?'}，已标记为异常",
        )
    )

    slow = by_id.get("GET-/slow#normal")
    checks.append(
        (
            "慢接口被识别",
            bool(slow) and slow.get("slow") is True,
            f"GET /slow 耗时 {slow['elapsed_ms'] if slow else '?'} ms",
        )
    )

    # 5) 退出码
    checks.append(("引擎正常退出", code == 0, f"exit code = {code}"))

    all_ok = True
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} —— {detail}")
        all_ok = all_ok and ok

    print("=" * 60)
    print("总判定：" + ("全部通过 ✅" if all_ok else "存在失败项 ❌"))
    print(f"报告：{report_path}")
    print(f"用例集：{cases_path}")

    # 清理自检产物（保留报告供人工查看）
    for tmp in (spec_path,):
        try:
            os.remove(tmp)
        except OSError:
            pass

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
