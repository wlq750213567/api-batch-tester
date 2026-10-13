# API 接口批量测试（api-batch-tester）

> 给一份 OpenAPI/Swagger 文档或接口清单，自动跑完全部接口并告诉你哪几个有问题。

手动测接口太慢？把这件事交给它：解析 spec → 自动派生正常/异常/边界用例 → 并发发起真实 HTTP 请求 → 逐条断言（状态码、JSON 合法性、必需字段、响应耗时）→ 产出 Markdown 测试报告与可复现的 JSON 用例集。

## 特性

- **零第三方依赖**：标准库 `urllib` 实现，环境里已有 `requests` 会自动加速
- **三类用例自动派生**：正常（按 schema 填必填参数）、异常（抽必填/错类型/畸形 JSON）、边界（超长/越界/非法枚举）
- **并发真实请求**：可配并发数、超时、重试
- **结构化断言**：状态码、JSON 合法性、必需字段（对象顶层与数组元素都校验）、耗时
- **双产物输出**：Markdown 测试报告 + 可复现 JSON 用例集
- **自带端到端自检**：`scripts/self_test.py` 起本地 mock API 验证引擎真实行为（9/9 通过）
- **结论全部来自真实 HTTP 响应，绝不编造结果**

## 快速开始

```bash
# 1. 准备 spec（OpenAPI 3.x / Swagger 2.0 文件或 URL，或简易接口清单 JSON）
# 2. 跑批量测试
python3 scripts/api_batch_tester_tool.py run \
  --spec ./openapi.json \
  --base-url https://api.example.com \
  --concurrency 8 \
  --timeout 10 \
  --bearer "$API_TOKEN" \
  --report ./report.md

# 3. 端到端自检（起本地 mock API 验证引擎真实行为）
python3 scripts/self_test.py
```

## 目录结构

```
├── SKILL.md                    # 技能说明书（触发条件、用法、参数、验收标准）
├── _meta.json                  # 元数据（版本、分类、触发词、入口脚本）
├── scripts/
│   ├── api_batch_tester_tool.py  # 主引擎（run / probe / status / export）
│   └── self_test.py              # 端到端自检
├── references/
│   └── README.md               # 输入格式说明 + 简易清单样例
├── data/
│   └── sample-report.md        # 真实产出样例报告
├── icons/                      # 上架图标（PNG 512/1024 + SVG 源文件）
├── dist/                       # 技能包 zip（标准版 + 平铺版）
├── docs/                       # 上架物料、后台填写速答、双平台提交对照表
└── render_icon.py              # 图标复现脚本
```

## 完整参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `--spec` / `-s` | 必填 | OpenAPI/Swagger 文件或 URL（别名 `--input`） |
| `--base-url` | 取 spec 里的 servers | 覆盖服务地址 |
| `--include` | 全部 | 只测路径含指定子串的接口 |
| `--methods` | 全部 | 只测指定方法 |
| `--concurrency` / `-c` | 8 | 并发数 |
| `--timeout` / `-t` | 10 | 单请求超时（秒） |
| `--retries` | 1 | 网络错误重试次数 |
| `--max-cases` | 6 | 每个接口最多派生用例数 |
| `--bearer` | — | 写入 `Authorization: Bearer <token>` |
| `--apikey` | — | 写入 `X-API-Key` 头 |
| `--header` / `-H` | — | 附加请求头 |
| `--report` / `-o` | `data/report_<时间>.md` | Markdown 报告路径 |
| `--cases-out` | `data/cases_<时间>.json` | 用例集与结果路径 |
| `--slow-ms` | 2000 | 超过该耗时标记为"偏慢" |

## 能力边界

**能做**：并发回归测试、输入校验检查、慢接口识别、失败归因（超时 / 4xx / 5xx / 连接错误）。

**不做**：不做渗透测试或漏洞扫描；不绕过鉴权；不猜未在 spec 中声明的接口；不代替业务正确性判断（只校验结构，不校验业务语义）。

## 许可

MIT License
