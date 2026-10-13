# 参考资料

本目录存放 api-batch-tester 的输入格式说明与样例。

## 简易清单格式

不想写完整 OpenAPI？可以直接给一份简化 JSON：

```json
{
  "base_url": "https://api.example.com",
  "endpoints": [
    { "id": "health", "method": "GET", "path": "/health" },
    { "id": "listUsers", "method": "GET", "path": "/users",
      "params": [ { "name": "page", "in": "query", "required": true,
                    "schema": { "type": "integer", "minimum": 1 } } ] },
    { "id": "createUser", "method": "POST", "path": "/users",
      "body_schema": { "type": "object", "required": ["name"],
                       "properties": { "name": { "type": "string" } } },
      "expect_status": [201] },
    { "id": "search", "method": "GET", "path": "/search",
      "params": [ { "name": "q", "in": "query", "required": true,
                    "schema": { "type": "string", "maxLength": 50 } } ] }
  ]
}
```

字段说明：

| 字段 | 必需 | 说明 |
|---|---|---|
| `base_url` | 是 | 服务根地址，也可用 `--base-url` 覆盖 |
| `endpoints[].method` | 否 | 默认 `GET` |
| `endpoints[].path` | 是 | 路径，支持 `{id}` 占位 |
| `endpoints[].params` | 否 | 与 OpenAPI parameter 同构（`name` / `in` / `required` / `schema`） |
| `endpoints[].body_schema` | 否 | 请求体 JSON Schema |
| `endpoints[].expect_status` | 否 | 期望状态码；不填则按 2xx 判定 |

## 支持的 spec 来源

- 本地 `.json` / `.yaml` 文件
- 直接给 URL（如 `--spec https://petstore3.swagger.io/api/v3/openapi.json`）
  —— 需要当前网络能访问到该地址

## 样例产出

见技能目录 `data/sample-report.md`，那是对一份 demo spec 跑出的真实报告
（含执行摘要、接口明细、失败用例明细、最慢接口 Top 5、行动建议）。
