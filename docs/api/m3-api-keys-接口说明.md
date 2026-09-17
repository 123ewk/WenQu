# M3 · API Key 接口说明(前端对接文档)

> **适用页面**:07-api-keys。**机器契约**:`docs/api/openapi.json`(字段/枚举唯一事实源)。
> 本文只写流程、边界与前端要点;与契约冲突时以契约为准,拿不准以实测为准。

---

## 1. 概念与取值(前端渲染要用)

- **Key 形态**:`sk-live-` 前缀 + 43 位随机串。列表里只显示 `key_hint`(形如
  `sk-live-****ab12`),**完整明文只在创建响应出现一次**,之后任何接口都拿不回。
- **能力 `capabilities`**(与原型页 07 两个勾选项一一对应):

| 值 | 原型文案 | 允许的路由 |
|---|---|---|
| `chat` | 对话检索 | `POST /ask`、`POST /retrieval/search` |
| `documents` | 文档管理 | KB 列表/详情(读)、文档 上传/列表/详情/删除/重解析、分块列表/单块 |

- **知识库范围 `kb_ids`**:空数组 = 全部知识库(原型「全部」勾选);非空 = 只能碰
  这些 KB。范围约束对 Key 生效,**对页面正常登录用户完全无感**。
- **吊销**:软删除(`revoked_at` 置值)。列表包含已吊销的 Key,前端建议置灰展示;
  吊销后用该 Key 的程序立即收到 401。

## 2. 端点(均挂在空间下,角色门槛同页面权限)

| 方法/路径 | 说明 |
|---|---|
| `GET /api/v1/spaces/{space_id}/api-keys` | 列表(含已吊销);Viewer+ |
| `POST /api/v1/spaces/{space_id}/api-keys` | 创建,**201 返回明文仅此一次**;Editor+ |
| `DELETE /api/v1/spaces/{space_id}/api-keys/{key_id}` | 吊销(幂等,204);Editor+ |

### 创建请求

```json
{
  "name": "ci-pipeline",
  "description": "CI 构建后同步接口文档",
  "capabilities": ["chat"],
  "kb_ids": ["<kb_uuid>", "..."]      // 空数组 = 全部知识库
}
```

- `capabilities` 里的未知值 → `422 VALIDATION_ERROR`;`name` 缺失/超长 → 422。
- 空 `capabilities` 合法(创建出一把"什么都不能做"的 Key),前端默认勾选对话检索即可。

### 创建响应(201)

```json
{
  "id": "<key_uuid>",
  "name": "ci-pipeline",
  "description": "CI 构建后同步接口文档",
  "key_hint": "sk-live-****ab12",
  "capabilities": ["chat"],
  "kb_ids": ["<kb_uuid>"],
  "revoked_at": null,
  "last_used_at": null,
  "created_at": "2026-09-17T03:00:00Z",
  "plaintext": "sk-live-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

**前端要点**:这个 `plaintext` 就是原型里「Key 已创建,请立即复制保存,这是唯一一次
完整展示」的数据源;列表接口没有该字段,不要试图刷新后再取。

### 列表项

同上但**没有 `plaintext`**;`revoked_at` 非 null 即已吊销。

## 3. 用 Key 调接口(写进页面 07 的"使用说明"或文档提示)

程序方在请求头带 `X-API-Key: <明文>`(不是 Bearer):

```
POST /api/v1/spaces/{space_id}/retrieval/search
X-API-Key: sk-live-xxxx...
Content-Type: application/json

{"query": "检索词", "kb_ids": ["<kb_uuid>"]}   // kb_ids 可省略;Key 有范围时会自动收窄
```

行为边界(前端文案可直接引用):

| 场景 | 返回 |
|---|---|
| Key 缺失/无效/已吊销/创建者已离开空间 | `401 API_KEY_INVALID`(按凭据问题处理,重试无用) |
| 用 Key 访问未开放接口(如用户资料、成员管理) | `401 AUTH_REQUIRED`(Key 仅可用于上表能力路由) |
| Key 未授对应能力(如只有 chat 却调文档接口) | `403 API_KEY_CAPABILITY_DENIED` |
| 请求的 kb_ids 全在范围外 | `403 API_KEY_SCOPE_DENIED` |
| kb_ids 部分在范围内 | 放行,后端**自动收窄到交集**(不报错) |
| `kb_ids` 省略且有范围限制 | 等效于"只查范围内全部" |

- 401/403 都走统一错误壳;403 会被记录到空间审计(`access.denied`)。
- Key 以**创建者身份**行事:创建者被移出空间,Key 立即失效(401)。

## 4. 前端页面(07)检查单

- [ ] 列表列:名称 / Key(`key_hint`,mono 样式)/ 能力(badge:`chat`→对话检索、
      `documents`→文档管理)/ 允许的知识库(`kb_ids` 空 → "全部知识库",否则按 KB
      列表解析名称,取不到就显示数量)/ 创建时间 / 最近使用(`last_used_at` 为
      null 显示"从未使用")/ 操作(吊销;已吊销置灰)
- [ ] 新建弹窗:能力两个勾选项(默认勾对话检索)+ KB 范围多选(全部勾选时提交
      `kb_ids: []`)
- [ ] 创建成功弹窗:展示 `plaintext`,文案"仅此一次";关闭后列表只出现 `key_hint`
- [ ] 吊销二次确认:文案说明"立即 401、不可恢复"(见原型)
- [ ] 三态与权限:列表 loading/空/错误;`capabilities` 编辑入口按角色(Editor+)
      显隐;403/404 人话兜底
- [ ] 契约同步:`docs/api/openapi.json` 的 `ApiKeyOut` / `ApiKeyCreatedOut` /
      `CreateApiKeyRequest`
