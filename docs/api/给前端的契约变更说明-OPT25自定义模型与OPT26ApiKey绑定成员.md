# 契约变更说明:OPT-25 自定义模型 + OPT-26 API Key 绑定成员(给前端)

> **一句话:两处对前端有感知的变化 ——**
> ① **建 API Key 的 body 新增必填 `member_user_id`**(绑定成员),创建/吊销门槛从
> Editor+ 提到 **Admin+**;列表/创建响应加法式新增成员与签发者字段。
> ② `GET /api/v1/models` **响应形状完全不变**,数据源从内置 YAML 换成库内自定义
> 模型(管理端点 `/api/v1/admin/*` 为 superuser 专属,现有页面无需接入)。
> 机器契约以 `docs/api/openapi.json` 为准(已随本提交重导);本文讲契约看不出的部分。

---

## 1. OPT-26:API Key 改为"绑定空间成员"

### 1.1 为什么改

旧语义下 Key 挂在"签发者"身上,Key 能不能用、能干什么完全跟随签发者,
且 Editor 就能签发 —— 等于低权限用户可以把"自己的身份"打包成程序化凭据外发。
新语义:**Key 必须绑定一个现存空间成员,只有能拉成员的身份(Admin+)才能配 Key**,
校验时同时判定"Key 有效"与"绑定成员身份仍在、角色够不够"(双重校验)。

### 1.2 创建请求(唯一破坏性变化:新增必填字段)

```json
POST /api/v1/spaces/{sid}/api-keys
{
  "name": "ci-pipeline",
  "description": "CI 构建后同步接口文档",
  "member_user_id": "<uuid>",        // ★ 新增,必填:该空间的现存成员
  "capabilities": ["chat"],          // 不变
  "kb_ids": ["<kb_uuid>"]            // 不变(空数组 = 全部)
}
```

- `member_user_id` 缺失 → 422;指向非本空间成员 → 404 `MEMBER_NOT_FOUND`。
- 操作者角色 < Admin → 403 `FORBIDDEN`(Editor/Viewer 连创建带吊销都不行;
  **列表仍 Viewer+ 可看**)。
- 前端做法:新建弹窗加"绑定成员"下拉,数据源用现成的成员列表接口
  (`GET /spaces/{sid}/members`);默认选中当前用户。

### 1.3 响应新增字段(加法式,`ApiKeyOut`/`ApiKeyCreatedOut` 同步)

| 字段 | 含义 | 渲染建议 |
|---|---|---|
| `member_user_id` | 绑定成员 id(存量旧 Key 回退签发者;成员注销后为 null) | 列"绑定成员" |
| `member_username` / `member_nickname` | 绑定成员名字(注销后为 null) | `昵称(用户名)`;null 显示"已注销"并置灰该 Key |
| `member_role` | 绑定时点的空间角色数值(40/30/20/10) | badge,同成员页口径 |
| `created_by` / `created_by_name` | 签发操作者(仅留痕,不参与运行时身份) | 列"签发人" |

`plaintext` 语义不变:只在创建响应出现一次。

### 1.4 运行时语义变化(影响"使用说明"文案)

- **Key 以绑定成员身份行事**:该成员被移出空间/注销 → Key 立即 401
  `API_KEY_INVALID`;签发者被移出**不影响** Key。
- 成员在空间内**被降级** → Key 的权限实时收敛(高权限路由由服务层 403
  `FORBIDDEN`,与真人同构);能力/KB 范围限制仍在其之上(先 401/403 链路口径不变):
  未授能力 403 `API_KEY_CAPABILITY_DENIED`、范围外 403 `API_KEY_SCOPE_DENIED`。
- 旧文案"创建者被移出空间,Key 立即失效"要改为"**绑定成员**被移出,Key 立即失效"。

## 2. OPT-25:模型清单改为库内自定义模型

- `GET /api/v1/models` **形状逐字段不变**(chat/embedding/rerank 三个数组 +
  `defaults`)—— 现有模型选择器零改动。区别只在:清单内容改由管理端
  (superuser)在 `/api/v1/admin/model-providers` / `/api/v1/admin/models` 配置,
  内置 models.yaml 下线。
- **空清单是合法状态**(新部署还没配模型):`chat/embedding/rerank` 返回空数组、
  `defaults` 为 `{}` —— 选择器按现有"空数据"分支渲染即可,不要当错误。
- 调用侧兜底口径不变:选中的模型被停用/删除后,`/ask`、检索仍可能收到
  503 `MODEL_NOT_CONFIGURED`(消息会点名是 chat 还是 embedding 未配置),
  需继续展示该错误。
- `/api/v1/admin/*` 模型管理端点**不在本轮前端接入范围**(superuser 专属、
  API Key 不可用);将来要做管理页再提,现在无需处理。
- `POST /kb` 的 `embedding_model` 可不传(服务端取默认嵌入模型);契约本就如此,无变化。

## 3. 前端要做的(汇总)

1. 07-api-keys 新建弹窗:加"绑定成员"必填下拉;页面按 Admin+ 显隐创建/吊销入口
   (Editor 只见列表);
2. 列表加"绑定成员"/"签发人"两列(字段见 §1.3);"已注销成员"的 Key 置灰;
3. 文案修订:"创建者离开 → 失效"改为"绑定成员离开 → 失效";
4. 模型选择器:兼容空清单渲染(不报错);其余零改动;
5. 类型同步:`CreateApiKeyRequest` / `ApiKeyOut` / `ApiKeyCreatedOut` 以重导的
   openapi.json 为准。

## 4. 后端落地与验证(供实测对照)

迁移 0012(`model_providers`/`custom_models` 建表 + `api_keys.member_user_id` 回填
= created_by)→ 协议注册表 + DB 目录 → `/api/v1/admin/*` 端点 → API Key 绑定成员;
测试:服务单测 + HTTP 集成(角色门槛、双重校验、存量回退)+ 管理端点 6 例集成 +
契约门禁;冒烟脚本 `go_live_smoke.py` 已按新语义适配(条 5 建 Key 带 member_user_id,
条 6 新增"Viewer 建 Key 403"检查)。
