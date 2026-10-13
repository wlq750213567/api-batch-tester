---
name: api-batch-tester
description: "API 接口批量测试与回归体检。给一份 OpenAPI/Swagger 文档或接口清单，自动派生正常/异常/边界用例，并发发起真实请求并逐条断言（状态码、JSON 合法性、必需字段、响应耗时），产出 Markdown 测试报告与可复用用例集。触发词：API测试、接口测试、批量测试、接口回归、接口体检、Swagger测试、OpenAPI测试、接口压测。"
---

# API 接口批量测试

> 手动测接口太慢？给一份 spec，自动跑完全部接口并告诉你哪几个有问题。

## 何时使用

- 「帮我把这几个接口都测一遍」「按这份 Swagger 测一下」「接口回归」
- 「上线前体检一下接口」「哪个接口挂了 / 哪个最慢」
- 接口改版后需要确认老接口没被改坏

## 前置依赖

**无需安装任何第三方库即可运行**（标准库 urllib 实现）。
如果环境里已有 `requests`，引擎会自动优先使用它：

```bash
pip install requests   # 可选，装了更快、超时控制更精细
```

## 使用流程

### 步骤 1：准备 spec

接受三种输入，任选其一：

| 类型 | 说明 |
|---|---|
| OpenAPI 3.x | `.json` / `.yaml` 文件，或一个 spec URL |
| Swagger 2.0 | 同上 |
| 简易清单 | `{"base_url": "...", "endpoints": [{"method":"GET","path":"/users"}]}` |

### 步骤 2：跑批量测试

```bash
python3 scripts/api_batch_tester_tool.py run \
  --spec ./openapi.json \
  --base-url https://api.example.com \
  --concurrency 8 \
  --timeout 10 \
  --bearer "$API_TOKEN" \
  --report ./report.md
```

### 步骤 3：读结果并汇报

脚本 stdout 会返回 JSON 摘要（`headline` 一句话结论 + `summary` 指标 + 报告路径）。
**向用户汇报时必须包含**：测了几个接口、几个有问题、最需要关注的是哪个、建议动作。

## 参数说明

| 参数 | 默认 | 作用 |
|---|---|---|
| `--spec` / `-s` | 必填 | OpenAPI/Swagger 文件或 URL（别名 `--input`） |
| `--base-url` | 取 spec 里的 servers | 覆盖服务地址，常用于指向测试环境 |
| `--include` | 全部 | 只测路径含指定子串的接口，逗号分隔，如 `/users,/orders` |
| `--methods` | 全部 | 只测指定方法，如 `GET,POST` |
| `--concurrency` / `-c` | 8 | 并发数 |
| `--timeout` / `-t` | 10 | 单请求超时（秒） |
| `--retries` | 1 | 网络错误重试次数 |
| `--max-cases` | 6 | 每个接口最多派生多少条用例 |
| `--bearer` | — | 写入 `Authorization: Bearer <token>` |
| `--apikey` | — | 写入 `X-API-Key` 头 |
| `--header` / `-H` | — | 附加请求头，可重复，如 `-H "X-Env: test"` |
| `--report` / `-o` | `data/report_<时间>.md` | Markdown 报告输出路径 |
| `--cases-out` | `data/cases_<时间>.json` | 用例集与结果输出路径 |
| `--slow-ms` | 2000 | 超过该耗时标记为"偏慢" |

另有 `probe`（探单个 URL）、`status`（看产物）、`export`（列产物）三个子命令：

```bash
python3 scripts/api_batch_tester_tool.py probe https://api.example.com/health
```

## 它到底测了什么

对每个接口自动派生三类用例：

1. **正常用例** —— 按 spec 的 schema 填好必填参数与请求体，期望 2xx
2. **异常用例** —— 抽掉必填参数 / 传错类型 / 发畸形 JSON，期望被拒绝（非 2xx）
3. **边界用例** —— 超长字符串、越界数值、非法枚举，期望被拒绝

每条用例的断言：

- 状态码是否符合 spec 声明
- 2xx 响应是否为合法 JSON（当 spec 声明返回 JSON 时）
- 响应是否包含 spec 声明的必需字段（对象顶层与数组元素都校验）
- 响应耗时（超阈值仅提示，不计失败）

## 输出格式

- **Markdown 测试报告**：执行摘要、接口明细表、失败用例明细（含请求 URL、实际状态码、响应片段）、最慢接口 Top 5、行动建议
- **JSON 用例集**：`{meta, summary, results[]}`，每条含用例意图、请求、状态码、耗时、断言明细，**可直接复用于回归对比**

## 验收标准

- ✅ 报告里的每个数字都来自真实 HTTP 响应，无占位符
- ✅ 正常接口与问题接口能被区分判定
- ✅ 失败用例给出可定位的信息（URL + 状态码 + 响应片段）
- ✅ 用例集可复现，便于下次回归 diff

## 能力边界

**能做**：并发回归测试、输入校验检查、慢接口识别、失败归因（超时 / 4xx / 5xx / 连接错误）。

**不做**：不做渗透测试或漏洞扫描；不绕过鉴权；不猜未在 spec 中声明的接口；不代替业务正确性判断（只校验结构，不校验业务语义）。

## 注意事项

- 所有结论必须来自真实请求，**严禁编造测试结果**；请求失败时如实报告失败原因
- spec 与实现不一致时（如 spec 说返回 200 实际 500），如实标记为异常，交由用户判断哪边该改
- 目标为生产环境时，先提醒用户并发数不宜过高
- 报告必须落盘为文件，不能只在对话里输出

## 自检

技能自带端到端自检（起本地 mock API 验证引擎真的在发请求、真的在断言）：

```bash
python3 scripts/self_test.py
```
