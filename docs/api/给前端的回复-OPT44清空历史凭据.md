# 给前端的回复 · OPT-44 删档死锁与"清空历史凭据"

日期:2026-09-23。对应工单:台账 §2 的 **OPT-44**(由 OPT-41 F6/dev 联调实测暴露)。
契约真相源:`docs/api/给前端的契约变更说明-OPT41权限档位.md` §5、`docs/api/m3-api-keys-接口说明.md`。
本轮前后端都由我改,所以这份文档既是"给前端的回复",也是改动清单。

一句话结论:**OPT-44 已修完,本地分三片提交 —— 后端与测试 `048ee6f`、测试隔离 `2dfb76f`、前端与文档(含本文)为第三片。
门禁全绿:后端 ruff/mypy 干净、`pytest` 505 passed;前端 `npm run build` 通过。
真机链路已在临时库 + 临时后端 + 真实浏览器各跑通一遍,含 1 个反向用例。**
灰度开关仍为默认关闭,本轮没有动它,也没有合 main。

## 0. 先解释几个本文反复出现的词

- **档位 / 权限档位**:`permission_groups` 表里的一行,一份"能做什么 + 能看哪些库"的权限包。OPT-41 起它是权限的唯一真相。
- **凭据 / 历史凭据**:这里指 **API Key 的台账行**(`api_keys` 表)。**明文 Key 从来没存进库**,库里只有哈希和 `key_hint`(形如 `sk-live-****ddbA`,只用来让人认出是哪一把)。所以"清空历史凭据"清的是台账行,不是"删掉一个还能用的密码"。
- **吊销**:`DELETE /api-keys/{id}` —— 只把 `revoked_at` 置成当前时间,**行还在**。这是 Key 唯一的下线手段。
- **清空(purge)**:OPT-44 新加的 `DELETE /permission-groups/{gid}/revoked-keys` —— 把本档位下**已吊销**的台账行**真的删掉**,删之前逐把写一条审计快照。在用的 Key 一把都不碰。
- **外键 RESTRICT**:`api_keys.group_id` 指向档位,数据库层面禁止"档位下面还挂着 Key 就把档位删掉"。这是刻意留的 fail-closed 护栏,本轮**没有改它**。
- **审计快照**:删行之前先把这把 Key 的归属信息(`key_hint`、所属档位、归因成员、签发人、创建/吊销/最近使用时间)写进 `audit_logs`。行一删,这就是唯一的证据。

## 1. 已完成

### 1.1 后端(commit `048ee6f`)

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| 新审计动作 `api_key.purged` | `server/src/app/domain/enums.py` | 值就是字符串 `api_key.purged` |
| 仓储补两个方法 | `domain/interfaces.py`、`application/repository/api_keys.py` | `list_for_group(group_id)`、`delete(api_key)` |
| 删档守卫:计数拆分 + 文案只提真实动作 | `application/service/permission_groups.py:174-217` | `details` 现在给 `key_count` / `active_key_count` / `revoked_key_count` / `member_count` |
| 新方法 `purge_revoked_keys()` | 同文件 `:219-259` | 只删 `revoked_at is not None` 的行;先写快照后删;返回删除条数 |
| 新端点 | `api/routes/permission_groups.py:184` | `DELETE /api/v1/spaces/{sid}/permission-groups/{gid}/revoked-keys` → `{"deleted_count": N}` |
| 契约重导 | `docs/api/openapi.json` | +74 行,只有新端点与新枚举 |

要点(前端只需要知道这些):

1. **鉴权**:这个端点和档位页其他端点一样**只认 JWT**,`Admin+`。带 `X-API-Key` 打过来是 **401 `AUTH_REQUIRED`**(不是 403)。
2. **幂等**:档位下没有已吊销 Key 时返回 `{"deleted_count": 0}`,不报错;重复调用第二次也是 0。
3. **删档 400 的新文案**(服务端成品,可直接直显):
   - 只有已吊销的:`该档位下还有 1 把 API Key(1 把已吊销的先清空历史凭据),处理完才能删档`
   - 还有在用的:`该档位下还有 1 把 API Key(1 把在用的先吊销),处理完才能删档`
   - 两种都有:`…(1 把在用的先吊销、1 把已吊销的先清空历史凭据),处理完才能删档`
   - **旧文案里的"或改指到别的档位"已经删掉** —— 项目里没有"改 Key 所属档位"的端点,留着是第二个空头承诺。
4. **`member_count` 单独一条 400**:档位还挂着成员时文案是 `该档位还挂着 N 个成员,请先解除分配`,`details` 只有 `group_id` / `member_count`,**不含 Key 计数**。清空凭据解决不了成员占用,别把两步合成一个按钮。

### 1.2 测试(commit `048ee6f`)

- 服务层单测:**7 条新增** + 原删档用例补 `active/revoked` 计数断言(`tests/test_permission_group_service.py`)。钉住的行为:计数拆分正确、只碰已吊销、逐把留快照、快照字段齐全、清空后删档成功、空档位返回 0、非 Admin 403、跨空间 404。
- HTTP 集成测:**5 条**(`tests/test_permission_group_integration.py`,真 Postgres)。含 `400 details → purge → 审计 → 204` 的完整闭环,以及"在用 Key 绝不被清""带 Key 打是 401"。
- 契约门禁:`tests/test_openapi_contract.py` 的 `REQUIRED_PATHS` 已登记新路径。**注意它是双向门禁** —— 契约里有但清单没有、或清单里有但契约没有,都会红。

### 1.3 测试隔离修复(commit `2dfb76f`,与 OPT-44 无关,单独一片便于回退)

`tests/test_config.py::test_prod_missing_config_refuses_to_start` 原先只传 `_env_file=None`,那只挡得住 **.env 文件**,挡不住**进程环境**里的同名变量。集成测试必须 source `.env` 才能连库,于是这条用例在开发机上必红。现在用 `monkeypatch.delenv` 显式清掉四个必填项再断言 fail-closed。**不影响运行**,只影响"本机跑门禁会不会红"。

### 1.4 前端

| 文件 | 改动 |
| --- | --- |
| `web/src/api/types.ts` | 新增 `PurgeRevokedKeysOut`、`GroupDeleteBlockedDetails` |
| `web/src/api/permissionGroups.ts` | 新增 `apiPurgeGroupRevokedKeys()`、`groupDeleteBlockedDetails(error)`(从统一错误壳里取四个计数) |
| `web/src/views/permissionGroups/PermissionGroupsView.vue` | 删档改为 `deleteGroupWithPurgeRetry()`:被**只剩已吊销凭据**挡住时弹第二层确认,确认后清空并重试删档 |
| `web/src/views/audit/AuditView.vue` | `ACTION_LABELS` 加 `'api_key.purged': '清空历史凭据'`;顺手订正文件头注释里"带 Key 打只认 JWT 端点 = 403"的旧错口径(实测是 401) |

交互规则(评审时请重点看这两条):

- **只有 `revoked_key_count > 0` 且 `active_key_count === 0` 才递第二层确认**。还挂着在用的 Key 时**不弹**,只直显服务端 message —— 吊销哪一把是人的决定,前端不代做。
- 第二层确认的"取消"是**安全出口**:提示"未清空历史凭据,档位保留",不发任何请求,档位留着。

### 1.5 实测清单

**A. 真机 HTTP(临时库 `wenqu_opt44_i6` + 临时后端 `:8123`,灰度关)**

| # | 步骤 | 期望 | 实测 |
| --- | --- | --- | --- |
| 1 | 建档 → 签 Key → 吊销 → 删档 | 400,details 三个计数分开 | `{"key_count":1,"active_key_count":0,"revoked_key_count":1}` + 成品文案 |
| 2 | `DELETE …/revoked-keys` | `{"deleted_count":1}` | 通过 |
| 3 | 再调一次 | `{"deleted_count":0}` | 通过 |
| 4 | 删档 | 204 | 通过 |
| 5 | 再查该档 | 404 | 通过 |
| 6 | 审计 | 有 `api_key.purged`,快照字段齐 | `key_hint`/`group_id`/`group_name`/`owner_user_id`/`created_by`/`created_at`/`revoked_at`/`last_used_at` 全在 |
| 7 | 带 Key / 不带凭据打清空端点 | 401 | 两个都是 401 `AUTH_REQUIRED` |
| 8 | 档位下留一把**未吊销** Key 再清空 | `deleted_count:0`,Key 还在 | 通过 |

**B. 浏览器实测(临时前端 `:5274` 代理到 `:8123`,登录夹具账号)**

| # | 用例 | 结果 |
| --- | --- | --- |
| 1 | 档位页点"删除" → 首确认 → 断言第二层弹窗文案与按钮"清空 1 把并重试" | 通过 |
| 2 | 第二层点"取消" | 通过:提示"未清空历史凭据,档位保留",列表该行仍在,无请求发出 |
| 3 | 第二层点确认 | 通过:toast"档位已删除",列表变空态 |
| 4 | 审计页 | 通过:显示中文 label「**清空历史凭据**」(不是英文码),时间线上 创建 → 吊销 → 清空 → 删除档位 四条齐全 |
| 5 | 反向:档位下挂一把**未吊销** Key 删档 | 通过:只直显服务端 message「该档位下还有 1 把 API Key(1 把在用的先吊销),处理完才能删档」,**不弹第二层确认** |
| 6 | 控制台 | 只有两条预期的 400 资源日志,无其他 error/warn |

实测过程中改掉的两处前端缺陷(已含在提交里):

1. 第二层弹窗文案里写了 `**已吊销**`,而 `ElMessageBox` 不解析 Markdown,**星号会原样显示在界面上** → 去掉星号。
2. 同一句写死"清空后**这把** Key 的归属…",而 `revoked_key_count` 可以大于 1 → 改成"这些 Key"。

## 2. 未完成(以及为什么不在这轮做)

1. **不改 `api_keys.group_id` 的外键,不做级联删除。** 改成 `CASCADE` 能让"删档"一步成功,但那等于让一次点击连带抹掉一批程序化凭据的台账 —— 与 OPT-41 定的 fail-closed 相反。现在的形状是:数据库仍然拦你,想清楚了自己调清空。
2. **不提供"改指到别的档位"的端点。** 需求上说得通(换档不换 Key),但它会引入"一把 Key 的权限范围在没人重发凭据的情况下被换掉"这条新语义,和"档位是权限唯一真相"叠起来要单独设计。本轮只把文案里那句兑现不了的承诺删掉。
3. **清空端点不做批量(跨档位/整空间)。** 一次一把档,配合前端的二次确认;批量清没有对应的真实场景,只有误操作面。
4. **`api_key.purged` 的 detail 不进"改前/改后"那一类渲染。** 它是删除快照,不是配置变更,`AuditView` 的 `detailText()` 平铺渲染已够用。
5. **灰度开关仍为关**(`APP_REQUIRE_PERMISSION_GROUP_FOR_CORE=0`),本轮未验证开启态。

## 3. 能测 / 禁测

**能测**(请在本机新建空间或用例专用空间,别拿真实数据):

- 契约对表:`GET /openapi.json` 里 `DELETE /api/v1/spaces/{space_id}/permission-groups/{group_id}/revoked-keys` 的响应 `{deleted_count}`,以及删档 400 `details` 的四个计数键。
- 新建档位 → 签 Key → 吊销 → 删档(400)→ 清空 → 删档(204)→ 审计四条齐全。
- 反向用例:档位下有未吊销 Key 时清空返回 0、Key 仍在;删档仍 400 且文案只提"先吊销"。
- 权限:非 Admin 成员 403;带 `X-API-Key` 打清空端点 401。
- 前端:第二层弹窗的取消路径、审计页中文 label。

**禁测**:

- **不要**在共享 dev 库 `wenqu` 上批量调清空端点。那库里有另一条线正在做的 dev 数据清理,台账行删掉就回不来(明文本来就没有,但 `key_hint` 与归因只剩审计)。要测请新建档位/新建空间,或另起临时库。
- 不要拿真实接入方在用的 Key 做实验:清空**不可逆**,且不需要先吊销就能删掉已吊销行。
- 本轮验证用的临时库 `wenqu_opt44_i6`、临时后端 `:8123`、临时前端 `:5274` 与临时 vite 配置**已全部拆除**,不可复现,请按上面步骤自建现场。

## 4. 测试报告格式要求

回测时请按用例给全这五项,方便直接并到实测文档:

1. **环境**:后端地址与库名(临时库请写库名)、前端地址、`APP_ENV`、灰度开关取值。
2. **前置数据**:档位名 / Key 名 / 是否已吊销(**只贴 `key_hint`,不要贴明文 Key**,也不要贴 `.env` 里的任何值)。
3. **请求**:方法 + 完整路径 + 关键请求头类型(`Authorization: Bearer` 还是 `X-API-Key`)。
4. **期望 vs 实际**:HTTP 码 + `error.code` + `details` 四个计数 + 文案是否直显。文案请整段贴,不要转述。
5. **收尾证据**:审计页该行的动作 label 与 detail 渲染结果(截图或文本均可)。
