#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api-batch-tester · 真实批量接口测试引擎 (v4.0.0)

用法速览
--------
    # 1) 按 OpenAPI / Swagger spec 批量测
    python api_batch_tester_tool.py run --spec ./openapi.json --base-url https://api.example.com

    # 2) 带鉴权、提高并发
    python api_batch_tester_tool.py run --spec ./openapi.json --bearer sk-xxxx --concurrency 12

    # 3) 只测部分接口
    python api_batch_tester_tool.py run --spec ./openapi.json --include /users,/orders

    # 4) 快速探单个地址
    python api_batch_tester_tool.py probe https://api.example.com/health

    # 5) 查看产物
    python api_batch_tester_tool.py status

设计约束
--------
* 零第三方依赖即可运行（标准库 urllib）；若环境已装 requests 则自动优先使用
* 所有结论均来自真实 HTTP 响应，不做任何推测
* 引擎跑完即退出；用例失败不影响退出码，除非显式加 --fail-on-error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

__version__ = "4.0.0"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "data"))

try:  # 可选加速：装了 requests 就用它，没装则退回标准库
    import requests  # type: ignore

    _HAVE_REQUESTS = True
except Exception:  # noqa: BLE001
    _HAVE_REQUESTS = False

DEFAULT_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 8
SLOW_MS = 2000.0  # 超过这个耗时标记为"偏慢"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _now_human() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_source(location: str) -> str:
    """从本地文件或 http(s) 地址读文本。"""
    if re.match(r"^https?://", location, re.I):
        req = urllib.request.Request(location, headers={"User-Agent": "api-batch-tester/4.0"})
        with _open_url(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read()
        return _decode(raw)
    with open(location, "rb") as fh:
        return _decode(fh.read())


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # 测试场景常遇到自签证书 / 内网域名，默认不因证书问题中断测试
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_OPENER = None


def _open_url(req: urllib.request.Request, timeout: float):
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()))
    return _OPENER.open(req, timeout=timeout)


def http_call(
    method: str,
    url: str,
    headers: dict | None = None,
    body: str | bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """发一次真实 HTTP 请求，永远返回结构化结果（不抛异常）。

    返回: {ok, status, reason, headers, text, elapsed_ms, error}
    """
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "api-batch-tester/4.0")
    method = method.upper()
    started = time.perf_counter()
    out = {
        "ok": False,
        "status": 0,
        "reason": "",
        "headers": {},
        "text": "",
        "elapsed_ms": 0.0,
        "error": "",
    }

    if _HAVE_REQUESTS:
        try:
            resp = requests.request(
                method, url, headers=headers, data=body, timeout=timeout, allow_redirects=True
            )
            out["status"] = resp.status_code
            out["reason"] = resp.reason or ""
            out["headers"] = {k.lower(): v for k, v in resp.headers.items()}
            out["text"] = resp.text
            out["ok"] = True
        except requests.exceptions.Timeout:
            out["error"] = f"timeout after {timeout}s"
        except requests.exceptions.RequestException as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
    else:
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=method)
        for key, val in headers.items():
            req.add_header(key, val)
        try:
            with _open_url(req, timeout=timeout) as resp:
                out["status"] = resp.status
                out["reason"] = resp.reason or ""
                out["headers"] = {k.lower(): v for k, v in resp.headers.items()}
                out["text"] = _decode(resp.read())
                out["ok"] = True
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也是有效响应
            out["status"] = exc.code
            out["reason"] = exc.reason or ""
            out["headers"] = {k.lower(): v for k, v in (exc.headers or {}).items()}
            try:
                out["text"] = _decode(exc.read())
            except Exception:  # noqa: BLE001
                out["text"] = ""
            out["ok"] = True
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            out["error"] = f"timeout/network: {reason}" if "timed out" in reason.lower() else reason
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"

    out["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return out


# --------------------------------------------------------------------------
# spec 解析
# --------------------------------------------------------------------------
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def load_spec(location: str) -> tuple[dict, str]:
    """返回 (spec_dict, format)。支持 JSON / YAML / 简易清单。"""
    text = read_source(location)
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
        except ImportError:
            raise SystemExit(
                "spec 不是合法 JSON，且环境未安装 pyyaml。\n"
                "请改用 JSON 格式的 spec：pip install pyyaml 或提供 .json 文件。"
            )
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"spec 解析失败：{exc}")

    if not isinstance(data, dict):
        raise SystemExit("spec 顶层必须是对象（object）。")

    if "openapi" in data:
        fmt = "openapi3"
    elif "swagger" in data:
        fmt = "swagger2"
    elif "endpoints" in data or "paths" in data:
        fmt = "simple" if "endpoints" in data else "swagger2"
    else:
        raise SystemExit("无法识别 spec 格式：既没有 openapi / swagger 字段，也没有 endpoints。")
    return data, fmt


def _normalize_server(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def detect_base_url(spec: dict, fmt: str) -> str:
    if fmt == "openapi3":
        servers = spec.get("servers") or []
        if servers and isinstance(servers[0], dict):
            return _normalize_server(servers[0].get("url", ""))
    elif fmt == "swagger2":
        host = spec.get("host", "")
        if host:
            scheme = (spec.get("schemes") or ["https"])[0]
            return _normalize_server(f"{scheme}://{host}{spec.get('basePath', '')}")
    elif fmt == "simple":
        return _normalize_server(spec.get("base_url", ""))
    return ""


def _schema_of(param: dict) -> dict:
    """兼容 OpenAPI3(schema) / Swagger2(直接字段)。"""
    if isinstance(param.get("schema"), dict):
        return param["schema"]
    return {k: param[k] for k in ("type", "format", "enum", "default", "example", "items", "minimum",
                                  "maximum", "minLength", "maxLength", "pattern") if k in param}


def extract_operations(spec: dict, fmt: str) -> list[dict]:
    """把 spec 归一化成统一的操作列表。"""
    ops: list[dict] = []

    if fmt == "simple":
        for item in spec.get("endpoints") or []:
            if not isinstance(item, dict):
                continue
            ops.append(
                {
                    "method": (item.get("method") or "GET").upper(),
                    "path": item.get("path") or item.get("url") or "/",
                    "operation_id": item.get("id") or item.get("name") or item.get("path", ""),
                    "summary": item.get("summary", ""),
                    "params": item.get("params") or [],
                    "request_body": item.get("body_schema") and {"required": True, "schema": item["body_schema"]},
                    "expect_status": item.get("expect_status"),
                    "responses": {},
                }
            )
        return ops

    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        # path 级公共参数
        common_params = path_item.get("parameters") or []
        for method in HTTP_METHODS:
            if method not in path_item:
                continue
            op = path_item[method]
            if not isinstance(op, dict):
                continue

            params = list(common_params) + list(op.get("parameters") or [])
            norm_params = []
            for p in params:
                if not isinstance(p, dict):
                    continue
                norm_params.append(
                    {
                        "name": p.get("name", ""),
                        "in": p.get("in", "query"),
                        "required": bool(p.get("required", False)),
                        "schema": _schema_of(p),
                    }
                )

            request_body = None
            if isinstance(op.get("requestBody"), dict):
                rb = op["requestBody"]
                content = rb.get("content") or {}
                schema = None
                for media, media_obj in content.items():
                    if isinstance(media_obj, dict) and isinstance(media_obj.get("schema"), dict):
                        schema = media_obj["schema"]
                        break
                request_body = {"required": bool(rb.get("required", False)), "schema": schema}

            ops.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": op.get("operationId") or f"{method.upper()} {path}",
                    "summary": (op.get("summary") or op.get("description") or "").strip()[:160],
                    "params": norm_params,
                    "request_body": request_body,
                    "responses": op.get("responses") or {},
                }
            )
    return ops


# --------------------------------------------------------------------------
# 用例生成
# --------------------------------------------------------------------------
def _sample_value(schema: dict, name: str = "") -> object:
    """按 schema 造一个"正常"取值。"""
    if not isinstance(schema, dict):
        return "test"
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    stype = schema.get("type")
    if stype == "integer":
        return int(schema.get("minimum", 1) or 1)
    if stype == "number":
        return float(schema.get("minimum", 1.0) or 1.0)
    if stype == "boolean":
        return True
    if stype == "array":
        item = schema.get("items") if isinstance(schema.get("items"), dict) else {"type": "string"}
        return [_sample_value(item, name)]
    if stype == "object":
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            required = schema.get("required")
            keys = [k for k in (required or []) if k in props] or list(props.keys())[:5]
            return {k: _sample_value(props.get(k) or {}, k) for k in keys}
        return {}
    fmt = schema.get("format", "")
    if fmt == "date":
        return "2026-01-01"
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "email":
        return "tester@example.com"
    if fmt == "uuid":
        return "00000000-0000-4000-8000-000000000000"
    if fmt in ("int32", "int64"):
        return "1"
    if re.search(r"(^|_)(id|uuid|key|code|no)($|_)", name, re.I):
        return "1"
    if re.search(r"(^|_)(name|title|keyword|q|query|search)($|_)", name, re.I):
        return "test"
    return "test"


def _bad_value(schema: dict) -> object:
    """按 schema 造一个"应当被拒绝"的取值。"""
    stype = (schema or {}).get("type")
    if stype in ("integer", "number"):
        return "not-a-number"
    if stype == "boolean":
        return "not-a-bool"
    if stype == "array":
        return "not-an-array"
    return 12345  # 数值塞给字符串型


def _extreme_value(schema: dict) -> tuple[object, str] | None:
    """边界值：超长字符串 / 越界数值 / 非法枚举。"""
    schema = schema or {}
    if schema.get("maxLength"):
        n = int(schema["maxLength"])
        return ("x" * (n + 8), f"字符串超长（>{n}）")
    if schema.get("maximum") is not None and schema.get("type") in ("integer", "number"):
        return (schema["maximum"] + 1, f"超过 maximum={schema['maximum']}")
    if schema.get("minimum") is not None and schema.get("type") in ("integer", "number"):
        return (schema["minimum"] - 1, f"低于 minimum={schema['minimum']}")
    if schema.get("enum"):
        return ("__not_in_enum__", "枚举非法值")
    return None


def _ok_status_set(op: dict) -> set[int]:
    declared = {int(k) for k in (op.get("responses") or {}) if str(k).isdigit()}
    two_xx = {s for s in declared if 200 <= s < 300}
    return two_xx or {200, 201, 202, 204}


def _render_path(path: str, path_params: dict[str, object]) -> str:
    out = path
    for key, val in path_params.items():
        out = out.replace("{" + key + "}", urllib.parse.quote(str(val), safe=""))
    # 未提供取值的路径参数，用占位值兜底，避免请求打到非法路径
    out = re.sub(r"\{[^}]+\}", "1", out)
    return out


def build_cases(op: dict, base_url: str, opts: dict) -> list[dict]:
    """为一个操作派生用例：正常 + 异常 + 边界。"""
    cases: list[dict] = []
    params = op.get("params") or []

    path_params = {p["name"]: _sample_value(p.get("schema"), p["name"]) for p in params if p.get("in") == "path"}
    base_path = _render_path(op["path"], path_params)
    url = (base_url or "").rstrip("/") + base_path

    query_ok: dict[str, object] = {}
    header_ok: dict[str, object] = {}
    for p in params:
        if p.get("in") == "query":
            query_ok[p["name"]] = _sample_value(p.get("schema"), p["name"])
        elif p.get("in") == "header":
            header_ok[p["name"]] = str(_sample_value(p.get("schema"), p["name"]))

    body_ok = None
    rb = op.get("request_body")
    if rb and rb.get("schema"):
        body_ok = _sample_value(rb["schema"], "body")

    def as_url(q: dict) -> str:
        if not q:
            return url
        pairs = []
        for key, val in q.items():
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)
            pairs.append(f"{urllib.parse.quote(str(key))}={urllib.parse.quote(str(val))}")
        return url + ("&" if "?" in url else "?") + "&".join(pairs)

    ok_status = _ok_status_set(op)
    base_case = {
        "operation_id": op["operation_id"],
        "endpoint": f"{op['method']} {op['path']}",
        "summary": op.get("summary", ""),
        "expect_ok_status": sorted(ok_status),
    }

    # --- 正常用例 ---
    cases.append(
        {
            **base_case,
            "id": f"{op['method']}-{op['path']}#normal",
            "kind": "normal",
            "expect": {"status_in": sorted(ok_status)},
            "request": {
                "method": op["method"],
                "url": as_url(query_ok),
                "headers": header_ok,
                "body": json.dumps(body_ok, ensure_ascii=False) if body_ok is not None else None,
            },
        }
    )

    if opts.get("max_cases", 6) <= 1:
        return cases

    # --- 异常用例：逐个抽掉一个必填的 query/header 参数 ---
    required_nonpath = [p for p in params if p.get("required") and p.get("in") in ("query", "header")]
    for p in required_nonpath:
        if len(cases) >= opts.get("max_cases", 6):
            break
        q, h = dict(query_ok), dict(header_ok)
        (q if p["in"] == "query" else h).pop(p["name"], None)
        cases.append(
            {
                **base_case,
                "id": f"{op['method']}-{op['path']}#missing-{p['name']}",
                "kind": "invalid",
                "note": f"缺少必填参数 {p['name']}（{p['in']}）",
                "expect": {"status_not_2xx": True},
                "request": {
                    "method": op["method"],
                    "url": as_url(q),
                    "headers": h,
                    "body": json.dumps(body_ok, ensure_ascii=False) if body_ok is not None else None,
                },
            }
        )

    # --- 异常用例：类型错误 ---
    typed = [p for p in params if p.get("in") == "query" and (p.get("schema") or {}).get("type") in ("integer", "number", "boolean")]
    if typed and len(cases) < opts.get("max_cases", 6):
        p = typed[0]
        q = dict(query_ok)
        q[p["name"]] = _bad_value(p.get("schema") or {})
        cases.append(
            {
                **base_case,
                "id": f"{op['method']}-{op['path']}#badtype-{p['name']}",
                "kind": "invalid",
                "note": f"参数 {p['name']} 传错类型",
                "expect": {"status_not_2xx": True},
                "request": {
                    "method": op["method"],
                    "url": as_url(q),
                    "headers": dict(header_ok),
                    "body": json.dumps(body_ok, ensure_ascii=False) if body_ok is not None else None,
                },
            }
        )

    # --- 异常用例：畸形 JSON body ---
    if body_ok is not None and len(cases) < opts.get("max_cases", 6):
        cases.append(
            {
                **base_case,
                "id": f"{op['method']}-{op['path']}#badjson",
                "kind": "invalid",
                "note": "请求体不是合法 JSON",
                "expect": {"status_not_2xx": True},
                "request": {
                    "method": op["method"],
                    "url": as_url(query_ok),
                    "headers": {**header_ok, "Content-Type": "application/json"},
                    "body": "{ this is not json ",
                },
            }
        )

    # --- 边界用例 ---
    bounded = None
    for p in params:
        if p.get("in") == "query":
            extreme = _extreme_value(p.get("schema") or {})
            if extreme:
                bounded = (p, extreme)
                break
    if bounded and len(cases) < opts.get("max_cases", 6):
        p, (val, why) = bounded
        q = dict(query_ok)
        q[p["name"]] = val
        cases.append(
            {
                **base_case,
                "id": f"{op['method']}-{op['path']}#boundary-{p['name']}",
                "kind": "boundary",
                "note": f"参数 {p['name']} {why}",
                "expect": {"status_not_2xx": True},
                "request": {
                    "method": op["method"],
                    "url": as_url(q),
                    "headers": dict(header_ok),
                    "body": json.dumps(body_ok, ensure_ascii=False) if body_ok is not None else None,
                },
            }
        )

    return cases


# --------------------------------------------------------------------------
# 断言
# --------------------------------------------------------------------------
_JSON_CT = ("application/json", "application/problem+json", "application/vnd.api+json", "text/json")


def _required_of(schema: dict | None) -> list[str]:
    """只在 spec 显式声明 required 时才作为校验目标，避免误报可选字段。"""
    if not isinstance(schema, dict):
        return []
    req = schema.get("required")
    return [str(x) for x in req] if isinstance(req, list) else []


def required_schema_info(schema: dict | None) -> dict:
    """区分「顶层对象必需字段」与「数组元素必需字段」。"""
    if not isinstance(schema, dict):
        return {"top": [], "items": []}
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return {"top": [], "items": _required_of(schema["items"])}
    return {"top": _required_of(schema), "items": []}


def evaluate(case: dict, resp: dict, opts: dict) -> dict:
    """对一次真实响应做断言。"""
    checks: list[dict] = []
    kind = case.get("kind", "normal")
    expect = case.get("expect") or {}
    status = resp.get("status", 0)

    if resp.get("error") and not resp.get("ok"):
        return {
            "passed": False,
            "checks": [{"name": "请求可达", "ok": False, "detail": resp.get("error", "")}],
            "fail_reason": "请求未完成：" + resp.get("error", ""),
        }

    # 1) 状态码
    if kind == "normal":
        want = expect.get("status_in") or []
        ok = status in want
        checks.append(
            {
                "name": "状态码符合预期",
                "ok": ok,
                "detail": f"实际 {status}，期望 {want}",
            }
        )
    else:
        ok = not (200 <= status < 300)
        checks.append(
            {
                "name": "非法输入被拒绝（非 2xx）",
                "ok": ok,
                "detail": f"实际 {status}" + ("" if ok else " ← 接口接受了非法输入，建议补校验"),
            }
        )

    # 2) 2xx 响应的结构断言
    if 200 <= status < 300:
        ctype = str(resp.get("headers", {}).get("content-type", "")).lower()
        wants_json = any(t in ctype for t in _JSON_CT)
        text = resp.get("text") or ""
        parsed = None
        if text.strip():
            try:
                parsed = json.loads(text)
                json_ok = True
            except json.JSONDecodeError:
                json_ok = False
            if wants_json:
                checks.append({"name": "响应是合法 JSON", "ok": json_ok, "detail": "JSON 解析" + ("成功" if json_ok else "失败")})
            req_info = case.get("_required") or {}
            if isinstance(parsed, dict):
                need = req_info.get("top") or []
                if need:
                    missing = [f for f in need if f not in parsed]
                    checks.append(
                        {
                            "name": "响应包含必需字段",
                            "ok": not missing,
                            "detail": ("缺失：" + ", ".join(missing)) if missing else f"共校验 {len(need)} 个字段",
                        }
                    )
            elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                need = req_info.get("items") or []
                if need:
                    missing = [f for f in need if f not in parsed[0]]
                    checks.append(
                        {
                            "name": "数组元素包含必需字段",
                            "ok": not missing,
                            "detail": ("缺失：" + ", ".join(missing))
                            if missing
                            else f"共校验 {len(need)} 个字段 × {len(parsed)} 个元素",
                        }
                    )

    # 3) 耗时
    elapsed = resp.get("elapsed_ms", 0.0)
    slow = elapsed > float(opts.get("slow_ms", SLOW_MS))
    checks.append(
        {
            "name": "响应耗时",
            "ok": True,  # 慢不算失败，仅提示
            "detail": f"{elapsed} ms" + ("（偏慢）" if slow else ""),
            "warn": slow,
        }
    )

    hard_fail = [c for c in checks if not c["ok"]]
    return {
        "passed": not hard_fail,
        "checks": checks,
        "fail_reason": "；".join(c["detail"] for c in hard_fail),
        "slow": slow,
    }


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def run_case(case: dict, opts: dict) -> dict:
    req = case["request"]
    headers = dict(req.get("headers") or {})
    body = req.get("body")
    if body is not None:
        headers.setdefault("Content-Type", "application/json")

    retries = int(opts.get("retries", 1))
    attempts = 0
    resp = {}
    while attempts <= retries:
        attempts += 1
        resp = http_call(
            req["method"],
            req["url"],
            headers=headers,
            body=body,
            timeout=float(opts.get("timeout", DEFAULT_TIMEOUT)),
        )
        if resp.get("ok") or resp.get("error", "").startswith("timeout") is False:
            break
        if attempts <= retries:
            time.sleep(min(0.5 * attempts, 2.0))

    verdict = evaluate(case, resp, opts)
    return {
        "id": case["id"],
        "endpoint": case["endpoint"],
        "kind": case["kind"],
        "note": case.get("note", ""),
        "method": req["method"],
        "url": req["url"],
        "status": resp.get("status", 0),
        "elapsed_ms": resp.get("elapsed_ms", 0.0),
        "attempts": attempts,
        "passed": verdict["passed"],
        "checks": verdict["checks"],
        "fail_reason": verdict.get("fail_reason", ""),
        "slow": verdict.get("slow", False),
        "response_excerpt": (resp.get("text") or "")[:400],
        "error": resp.get("error", ""),
    }


def exec_cases(cases: list[dict], opts: dict) -> list[dict]:
    concurrency = max(1, int(opts.get("concurrency", DEFAULT_CONCURRENCY)))
    results: list[dict] = []
    total = len(cases)
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(run_case, c, opts): c for c in cases}
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if opts.get("verbose"):
                print(f"  [{done}/{total}] 完成", file=sys.stderr)
    order = {c["id"]: i for i, c in enumerate(cases)}
    results.sort(key=lambda r: order.get(r["id"], 9999))
    return results


# --------------------------------------------------------------------------
# 统计与报告
# --------------------------------------------------------------------------
def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    k = (len(ordered) - 1) * pct
    low, high = int(k), min(int(k) + 1, len(ordered) - 1)
    frac = k - low
    return round(ordered[low] * (1 - frac) + ordered[high] * frac, 1)


def summarize(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    latencies = [r["elapsed_ms"] for r in results if r["status"]]

    by_endpoint: dict[str, dict] = {}
    for r in results:
        slot = by_endpoint.setdefault(
            r["endpoint"], {"endpoint": r["endpoint"], "total": 0, "passed": 0, "failed": 0, "lat": [], "last_status": 0, "notes": []}
        )
        slot["total"] += 1
        slot["passed" if r["passed"] else "failed"] += 1
        if r["status"]:
            slot["lat"].append(r["elapsed_ms"])
            slot["last_status"] = r["status"]
        if not r["passed"]:
            intent = r.get("note") or {
                "normal": "正常用例未通过",
                "invalid": "非法输入用例",
                "boundary": "边界用例",
            }.get(r["kind"], r["kind"])
            slot["notes"].append(f"{intent} —— {r.get('fail_reason') or '见失败明细'}")

    endpoints = []
    for slot in by_endpoint.values():
        lat = slot.pop("lat")
        slot["p95_ms"] = percentile(lat, 0.95) if lat else 0.0
        slot["avg_ms"] = round(statistics.mean(lat), 1) if lat else 0.0
        slot["verdict"] = "通过" if slot["failed"] == 0 else f"{slot['failed']} 项异常"
        endpoints.append(slot)
    endpoints.sort(key=lambda s: (s["failed"] == 0, -s["total"]))

    # 每个接口只保留最慢的一条，按接口去重后取 Top 5
    slowest: list[dict] = []
    seen_slow: set[str] = set()
    for r in sorted([x for x in results if x["status"]], key=lambda x: -x["elapsed_ms"]):
        if r["endpoint"] in seen_slow:
            continue
        seen_slow.add(r["endpoint"])
        slowest.append(r)
        if len(slowest) >= 5:
            break

    return {
        "total_cases": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "endpoints_tested": len(by_endpoint),
        "endpoints_with_issues": sum(1 for s in endpoints if s["failed"]),
        "latency_ms": {
            "avg": round(statistics.mean(latencies), 1) if latencies else 0.0,
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "endpoints": endpoints,
        "slowest": [
            {"endpoint": r["endpoint"], "url": r["url"], "status": r["status"], "elapsed_ms": r["elapsed_ms"]}
            for r in slowest
        ],
        "failures": [r for r in results if not r["passed"]],
        "runner": "requests" if _HAVE_REQUESTS else "urllib(标准库)",
    }


def render_report(meta: dict, summary: dict, results: list[dict]) -> str:
    lat = summary["latency_ms"]
    lines: list[str] = []
    add = lines.append

    add("# 📋 API 批量测试报告")
    add("")
    add(f"**生成时间**：{meta['generated_at']}  ")
    add(f"**Spec**：`{meta['spec']}`  ")
    add(f"**Base URL**：`{meta['base_url'] or '(来自 spec)'}`  ")
    add(f"**HTTP 引擎**：{summary['runner']}｜**并发**：{meta['concurrency']}｜**超时**：{meta['timeout']}s")
    add("")

    verdict = (
        "全部通过 ✅"
        if summary["failed"] == 0
        else f"发现 {summary['failed']} 项异常 ⚠️（涉及 {summary['endpoints_with_issues']} 个接口）"
    )
    add("## 一、执行摘要")
    add("")
    add(f"> **{verdict}**")
    add("")
    add("| 指标 | 数值 |")
    add("|---|---|")
    add(f"| 接口数 | {summary['endpoints_tested']} |")
    add(f"| 用例数 | {summary['total_cases']}（通过 {summary['passed']} / 失败 {summary['failed']}） |")
    add(f"| 通过率 | {summary['pass_rate']}% |")
    add(f"| 平均耗时 | {lat['avg']} ms |")
    add(f"| P95 / P99 | {lat['p95']} ms / {lat['p99']} ms |")
    add(f"| 最慢 | {lat['max']} ms |")
    add("")

    add("## 二、接口明细")
    add("")
    add("| 接口 | 用例 | 通过 | 异常 | 平均耗时 | P95 | 结论 |")
    add("|---|---|---|---|---|---|---|")
    for e in summary["endpoints"]:
        add(
            f"| `{e['endpoint']}` | {e['total']} | {e['passed']} | {e['failed']} | "
            f"{e['avg_ms']} ms | {e['p95_ms']} ms | {e['verdict']} |"
        )
    add("")

    if summary["failures"]:
        add("## 三、失败用例明细")
        add("")
        for r in summary["failures"]:
            add(f"### ❌ `{r['id']}`")
            add("")
            add(f"- **请求**：`{r['method']} {r['url']}`")
            add(f"- **实际状态码**：{r['status'] or '无响应'}（{r['error'] or '已收到响应'}）")
            if r.get("note"):
                add(f"- **用例意图**：{r['note']}")
            add(f"- **判定**：{r['fail_reason'] or '见下'}")
            checks = r.get("checks") or []
            for c in checks:
                if not c["ok"]:
                    add(f"  - ✗ {c['name']}：{c['detail']}")
            if r.get("response_excerpt"):
                add("- **响应片段**：")
                add("")
                add("```")
                add(r["response_excerpt"].replace("\n", " ")[:300])
                add("```")
            add("")

    if summary["slowest"]:
        add("## 四、最慢接口 Top 5")
        add("")
        add("| 接口 | 状态码 | 耗时 |")
        add("|---|---|---|")
        for s in summary["slowest"]:
            add(f"| `{s['endpoint']}` | {s['status']} | {s['elapsed_ms']} ms |")
        add("")

    add("## 五、行动建议")
    add("")
    adv: list[str] = []
    for e in summary["endpoints"]:
        for note in e["notes"][:3]:
            adv.append(f"- **`{e['endpoint']}`**：{note}")
    if lat["p95"] > SLOW_MS:
        adv.append(f"- 整体 P95 达 {lat['p95']} ms，建议排查慢接口的索引、缓存或下游依赖。")
    if not adv:
        adv.append("- 未发现异常，接口群当前表现健康。")
    adv.append("- 本报告基于真实 HTTP 响应生成；断言依据 spec 声明，spec 与实现不一致时请以业务为准。")
    lines.extend(adv)
    add("")

    add("---")
    add("")
    add("*由 api-batch-tester v4 生成 · 所有数据来自真实请求*")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_run(args) -> int:
    ensure_dirs()
    spec_loc = args.spec or args.input
    if not spec_loc:
        print(
            json.dumps(
                {"status": "error", "message": "缺少 --spec：请提供 OpenAPI/Swagger 文件或 URL"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    spec, fmt = load_spec(spec_loc)
    base_url = _normalize_server(args.base_url) or detect_base_url(spec, fmt)
    ops = extract_operations(spec, fmt)

    if args.include:
        wanted = [x.strip() for x in args.include.split(",") if x.strip()]
        ops = [o for o in ops if any(w in o["path"] or w in o["operation_id"] for w in wanted)]
    if args.methods:
        allowed = {m.strip().upper() for m in args.methods.split(",") if m.strip()}
        ops = [o for o in ops if o["method"] in allowed]
    if args.max_endpoints and len(ops) > args.max_endpoints:
        ops = ops[: args.max_endpoints]

    if not ops:
        print(json.dumps({"status": "error", "message": "spec 中没有匹配到任何接口"}, ensure_ascii=False, indent=2))
        return 2

    # 全局请求头
    global_headers: dict[str, str] = {}
    if args.header:
        for item in args.header:
            if ":" in item:
                k, v = item.split(":", 1)
                global_headers[k.strip()] = v.strip()
    if args.bearer:
        global_headers["Authorization"] = f"Bearer {args.bearer}"
    if args.apikey:
        global_headers["X-API-Key"] = args.apikey

    opts = {
        "concurrency": args.concurrency,
        "timeout": args.timeout,
        "retries": args.retries,
        "max_cases": args.max_cases,
        "slow_ms": args.slow_ms,
        "verbose": args.verbose,
    }

    cases: list[dict] = []
    for op in ops:
        for case in build_cases(op, base_url, opts):
            hdrs = dict(case["request"]["headers"])
            hdrs.update({k: v for k, v in global_headers.items() if k not in hdrs})
            case["request"]["headers"] = hdrs
            # 从 2xx 响应 schema 提取必需字段，供断言使用
            schema = None
            for code, resp_obj in (op.get("responses") or {}).items():
                if str(code).startswith("2") and isinstance(resp_obj, dict):
                    content = resp_obj.get("content") or {}
                    for media_obj in content.values():
                        if isinstance(media_obj, dict) and isinstance(media_obj.get("schema"), dict):
                            schema = media_obj["schema"]
                            break
                    if isinstance(resp_obj.get("schema"), dict):  # swagger2
                        schema = resp_obj["schema"]
            case["_required"] = required_schema_info(schema)
            cases.append(case)

    print(f"🔍 解析到 {len(ops)} 个接口，生成 {len(cases)} 条用例，开始执行……", file=sys.stderr)
    started = time.time()
    results = exec_cases(cases, opts)
    elapsed = round(time.time() - started, 2)

    summary = summarize(results)

    report_path = args.report or os.path.join(DATA_DIR, f"report_{_ts()}.md")
    cases_path = args.cases_out or os.path.join(DATA_DIR, f"cases_{_ts()}.json")
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(cases_path)), exist_ok=True)

    meta = {
        "generated_at": _now_human(),
        "spec": spec_loc,
        "base_url": base_url,
        "concurrency": opts["concurrency"],
        "timeout": opts["timeout"],
        "duration_s": elapsed,
    }

    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(render_report(meta, summary, results))
    with open(cases_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"meta": meta, "summary": {k: v for k, v in summary.items() if k != "failures"}, "results": results},
            fh,
            ensure_ascii=False,
            indent=2,
        )

    headline = (
        f"{summary['endpoints_tested']} 个接口 / {summary['total_cases']} 条用例，通过率 {summary['pass_rate']}%；"
        + (
            "全部通过"
            if summary["failed"] == 0
            else f"{summary['endpoints_with_issues']} 个接口存在异常，最需关注："
            + (summary["failures"][0]["endpoint"] if summary["failures"] else "")
        )
    )

    payload = {
        "status": "success",
        "report_file": report_path,
        "cases_file": cases_path,
        "duration_s": elapsed,
        "headline": headline,
        "summary": {
            "endpoints_tested": summary["endpoints_tested"],
            "endpoints_with_issues": summary["endpoints_with_issues"],
            "total_cases": summary["total_cases"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "pass_rate": summary["pass_rate"],
            "latency_ms": summary["latency_ms"],
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.fail_on_error and summary["failed"]:
        return 1
    return 0


def cmd_probe(args) -> int:
    resp = http_call("GET", args.url, timeout=args.timeout)
    payload = {
        "status": "success" if resp.get("ok") else "error",
        "url": args.url,
        "http_status": resp.get("status"),
        "elapsed_ms": resp.get("elapsed_ms"),
        "content_type": resp.get("headers", {}).get("content-type", ""),
        "error": resp.get("error", ""),
        "body_excerpt": (resp.get("text") or "")[:300],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if resp.get("ok") else 1


def cmd_status(args) -> int:
    ensure_dirs()
    files = sorted(f for f in os.listdir(DATA_DIR) if not f.startswith("."))
    print(
        json.dumps(
            {
                "skill": "api-batch-tester",
                "version": __version__,
                "scene": "API 接口批量测试",
                "http_engine": "requests" if _HAVE_REQUESTS else "urllib(标准库)",
                "data_dir": DATA_DIR,
                "data_files": files[-10:],
                "file_count": len(files),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_export(args) -> int:
    ensure_dirs()
    files = [os.path.join(DATA_DIR, f) for f in sorted(os.listdir(DATA_DIR)) if not f.startswith(".")]
    if getattr(args, "format", "json") == "json":
        print(json.dumps({"files": files, "count": len(files)}, ensure_ascii=False, indent=2))
    else:
        print("\n".join(files))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="API 接口批量测试（真实请求 + 真断言）")
    parser.add_argument("--version", action="version", version=f"api-batch-tester {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="按 spec 批量测试")
    p_run.add_argument("--spec", "-s", help="OpenAPI/Swagger 文件或 URL（也支持 --input）")
    p_run.add_argument("--input", "-i", help="--spec 的别名")
    p_run.add_argument("--base-url", dest="base_url", help="覆盖 spec 中的服务地址")
    p_run.add_argument("--include", help="只测路径包含这些子串的接口，逗号分隔")
    p_run.add_argument("--methods", help="只测指定方法，如 GET,POST")
    p_run.add_argument("--max-endpoints", dest="max_endpoints", type=int, default=0)
    p_run.add_argument("--concurrency", "-c", type=int, default=DEFAULT_CONCURRENCY)
    p_run.add_argument("--timeout", "-t", type=float, default=DEFAULT_TIMEOUT)
    p_run.add_argument("--retries", type=int, default=1)
    p_run.add_argument("--max-cases", dest="max_cases", type=int, default=6, help="每个接口最多生成多少条用例")
    p_run.add_argument("--slow-ms", dest="slow_ms", type=float, default=SLOW_MS)
    p_run.add_argument("--header", "-H", action="append", help="附加请求头，可重复，如 -H 'X-Token: abc'")
    p_run.add_argument("--bearer", help="Bearer Token")
    p_run.add_argument("--apikey", help="写入 X-API-Key 头")
    p_run.add_argument("--report", "-o", help="Markdown 报告输出路径")
    p_run.add_argument("--cases-out", dest="cases_out", help="用例集 JSON 输出路径")
    p_run.add_argument("--fail-on-error", dest="fail_on_error", action="store_true")
    p_run.add_argument("--verbose", "-v", action="store_true")

    p_probe = sub.add_parser("probe", help="快速探测单个 URL")
    p_probe.add_argument("url")
    p_probe.add_argument("--timeout", "-t", type=float, default=DEFAULT_TIMEOUT)

    sub.add_parser("status", help="查看产物状态")

    p_exp = sub.add_parser("export", help="列出产物文件")
    p_exp.add_argument("format", nargs="?", default="json")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "probe":
        return cmd_probe(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "export":
        return cmd_export(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
