# 给前端的回复 · OPT-41 权限档位前端落地与 F6 实测

日期:2026-09-22。对应工单:`docs/api/前端实现工单-OPT41权限档位.md`(F1–F6)。
契约真相源:`docs/api/给前端的契约变更说明-OPT41权限档位.md`。

一句话结论:**F1–F5 六片已提交,门禁全绿;F6 的契约对表(39 项)、灰度两轮(9 项)、浏览器实测(8 个用例)全部通过**,并按实测结果补了 3 处前端处理。**2026-09-23 用户点头后,共享 dev 库已升 0017→0018,新契约在真实 dev 库 + 真实模型配置上重跑了一遍(灰度关),结论见 §6A —— 全绿,另修掉两处文档写错的口径。**

## 1. 先解释几个本文反复出现的词(工单里没展开的)

- **档位**:一份"能做什么 + 能看哪些库"的权限包。OPT-41 之后它是权限的**唯一真相**;API Key 只是档位的一把可轮换凭据。
- **挂载**:某人(或某 Key)与某档位之间的分配记录。表 `permission_group_members` 里一行就是一次挂载。
- **当前档 / current**:一个人可以挂好几档,但**同一空间同一时刻只有一档生效**,那档叫"当前档"。所以 UI 上"分配"和"设为当前"是两个按钮。
- **灰度开关**:服务端环境变量 `APP_REQUIRE_PERMISSION_GROUP_FOR_CORE`。关着时没档位也能问(老行为);开着时网页用户也必须先有生效档位。**只影响网页(JWT)这条路,第三方 Key 一直要档位。**
- **动作闸 / 范围闸**:动作闸管"这类接口你调不调得动"(有没有 `chat`/`documents` 能力);范围闸管"能碰到哪几个知识库"。两闸都读档位,不读角色。
- **fail-closed(默认拒绝)**:能力清单为空 = 什么都不能做;`all_kbs=false` 且 `kb_ids=[]` = 一个库都检索不到。**空不是"全部"。**

## 2. 验证环境(刻意不碰别人的东西)

| 项 | 取值 | 为什么 |
| --- | --- | --- |
| 数据库 | 新建 `wenqu_opt41_f6`,升到 head=0018 | 当时共享 dev 库 `wenqu` 停在 0016,0018 要删 `api_keys` 三列,**那轮没动它**(2026-09-23 用户点头后已升 0018,真机联调见 §6A) |
| 后端 | `127.0.0.1:8123` | 8000 上已有别的会话在跑 dev 后端,不抢 |
| 前端 | `localhost:5273` | 5173 同样已被占用;代理目标临时指到 8123 |
| 收尾 | 跑完删临时配置、停进程、删临时库 | 见 §7 |

## 3. 契约对表(前端类型 ↔ 真实后端)

1. **`docs/api/openapi.json` 与运行中后端 `/openapi.json` 逐字段一致**(57 条路径,语义 diff 为空)—— 说明 D 片导出的契约没漂移,前端类型是照真契约写的。
2. 39 项断言全过,挑前端最依赖的几条:
   - `GET /permission-groups` 返回**裸数组**(不是 `{items}`),`created_at` 是字符串;`members` 建档时是 `[]`。
   - Key 行内联 `group` 快照(`id/name/capabilities/kb_ids/all_kbs`),旧字段 `capabilities`/`kb_ids`/`all_kbs`/`is_active` **确实不在了**。
   - `owner_username=null` 用来判"归因成员已失效";被移出但选"保留挂载"的人,`members[].role` 是 `null`。
   - 错误体是 `{success:false, error:{code,message,details}}`,`web/src/api/http.ts` 已把 `error.code/message` 摊平成 `ApiError.code/message`,所以页面里 `e.code` 分派可用。
   - 吊销一把 Key 不影响同档另一把;档位改了不重发 Key,`row.group` 立刻是新范围。
   - 审计 `permission_group.updated` 的改前/改后在 `detail` 里(`kb_scope_before/after`、`capabilities_before/after`),`AuditView` 的 `detailText()` 直接渲染成一行,不用后端再改。
3. 后端拒绝文案本身就是给人看的(点名了档位名,如「你当前的权限档位「单库档」无权访问请求的知识库」),因此把三类拒绝的话术改成**优先直显后端 message**,原来的固定文案退居兜底。

## 4. 灰度两轮(9 项断言,全过)

| 场景 | 期望 | 实测 |
| --- | --- | --- |
| 无生效档位 · 问答 | 403 `PERMISSION_GROUP_REQUIRED` | ✔ 403,**不是 401** —— 不会被 `http.ts` 的刷新链踢回登录页 |
| 无生效档位 · 检索 | 同上 | ✔ |
| 无生效档位 · 读库列表 | 同上 | ✔ **见 §5.1,这条是本轮新发现** |
| 档位零能力 · 问答 | 403 `API_KEY_CAPABILITY_DENIED` | ✔ |
| 勾选范围外的库 | 403 `API_KEY_SCOPE_DENIED` | ✔ |
| `chat` + 零库 | 403 `API_KEY_SCOPE_DENIED` | ✔ 文案「没有勾选任何知识库」 |
| `kb_ids=null` | 按档位实时展开 | ✔ 请求进到检索阶段(报的是"未配向量化 Key",说明闸门已放行) |
| 第三方 Key 走范围外库 / 无 chat 档 | 403 两个码 | ✔ ✔ |
| 管理面路由(空间、成员、档位、Key) | 只吃角色,不吃档位 | ✔ 未挂档的人仍能开档位页 —— 否则没法给自己配档 |

## 5. 本轮发现的三件事与处置

### 5.1 灰度开启后,"配档页"自己会读不到知识库列表(已在前端补提示)

`GET /spaces/{sid}/knowledge-bases` 也在双凭据强制清单里受**范围闸**约束。于是灰度一开、管理员自己又没挂档时:问答页库下拉空、档位页的 KB 勾选列表也空 —— 而这两处显示的是"当前空间还没有知识库",**把人指错了方向**(真实原因是"你没档").

处置(纯前端,不改后端):
- `ChatView.vue`:读库列表失败且码为 `PERMISSION_GROUP_REQUIRED` 时,范围标签显示「知识库范围:未分配档位,读不到库列表」,下拉与空态文案同步改口径。
- `PermissionGroupsView.vue`:同样条件下在建档/改档弹窗里给一行琥珀提示 + **自救路径**:先用「放开本空间全部知识库」开关建一档 → 到「成员」把自己设为当前 → 回本页就能逐个勾选。浏览器实测这条自救路径可用(自救档建好并设为当前后,库列表恢复可读,提示自动消失)。

### 5.2 三类权限拒绝的话术改成优先用后端 message

后端文案点名了档位与缺的那一项,比前端写死的更准。前端保留原文案作为拿不到 message 时的兜底。

### 5.3 "保留挂载"的人被移出空间后,「设为当前」按钮置灰

`members[].role === null` 的行本来就置灰显示「已移出空间」,但按钮还能点(点了只会吃一个 4xx)。现在直接禁用并给 title 说明,少一次无意义请求。

## 6. 还欠什么

1. ~~**真机 dev 联调**~~ —— **已完成(2026-09-23,用户点头升库之后)**,共享 dev 库 `wenqu` 升到 head=0018,
   新契约在真实数据 + 真实模型配置上重跑一遍,结论与遗留见 §6A。备份文件仍留着可回退(见 §6A 末行)。
2. ~~**`docs/api/m3-api-keys-接口说明.md` 与新契约矛盾**~~ —— **已随 G 片订正**(结论没变,原因写错了:那份文档里
   **根本没有 `is_active` / `PUT /{id}/activate` 段落**(OPT-31 当时就没同步对接文档),真正过期的是
   "能力与库范围挂在 Key 上"和 `member_user_id` 这套字段名。已按 OPT-41 重写 §1–§4:Key 的权限口径只从
   `group` 读、创建请求改 `group_id`+`owner_user_id`、行为边界表补 `API_KEY_SPACE_MISMATCH` 与
   `empty_scope`/`out_of_scope` 两个 `details.reason`、检查单改成"选档位"下拉。
   顺带把契约变更说明 §5 漏登记的 `API_KEY_SPACE_MISMATCH`(Key 专用跨空间闸,人链路不会出现)补进错误码表。
   **对前端无新增工作量**:这些本来就是 §2/§3 已冻结的契约,只是把第三份文档追上。
   1. **但那一版把一处口径写错了,已随 §6A 实测订正**:原先写"Key 打档位端点 → `403 API_KEY_CAPABILITY_DENIED
      + reason=route_not_grantable`"。实测是 **`401 AUTH_REQUIRED`** —— 档位端点、Key 管理端点、会话列表、
      `/users/*`、`/admin/*` 都**只认 JWT**,压根不看 `X-API-Key`,连授权表都进不到。`route_not_grantable`
      这条分支**当前无任何路径可达**(12 条接受 Key 的双认证路由与能力表的 12 个条目一一对应),它是给"以后新增双认证路由忘了登记"兜底的
      fail-closed 护栏,单测 `test_api_key_integration.py:165-178` 早就钉住了这个口径 —— 是我写文档时没跟代码。
      三份文档(契约说明 §2/§5、m3 §3、台账 §1.1)已统一改成这个说法。
3. 观察(既有行为,与 OPT-41 无关,未动):点「退出登录」后停在当前页不跳 `/login`,页面数据已清空、再点任何链接都会被路由守卫拦回登录,只是 URL 难看。**已登记为 §2 的 OPT-43**(不并进本批:动的是登出逻辑,混提会让回滚范围失真)。
4. 灰度开关 `APP_REQUIRE_PERMISSION_GROUP_FOR_CORE` 什么时候开 = 运维动作,**已登记为 §2 的 OPT-42**
   (硬前置:先给每个真人配档并设 current,否则开的那一刻全员禁用)。本批代码默认关,行为与今天逐字节一致。
5. **OPT-41 与本批全部结论已登记进 `docs/优化台账.md`** §1.1(含契约规模 57 路径/82 方法、破坏性字段清单、
   迁移 0017/0018),并把 **OPT-37 的迁移号段从"直接取 0017"改成"取 0019"**(0017/0018 被本项占用)。
6. **真机跑出来的一个语义问题(未动代码,待拍板)**:档位下只要**签发过** Key,这个档位就删不掉 ——
   删档守卫把**已吊销**的 Key 也计进 `key_count`,而"吊销"是 Key 唯一的下线手段(没有硬删端点),
   400 文案"请先吊销或改指到别的档位"里前半句是无效动作。根因是 `api_keys.group_id` 的 FK 是
   `ON DELETE RESTRICT`(直接删档会撞外键),所以这是**故意的 fail-closed**,不是漏了过滤;要动的是"怎么给它一条出路"。
   **对前端的影响**:这条 400 上不要放"去吊销"按钮,直显 message 就行。**已登记为 §2 的 OPT-44**(三个选项 + 推荐 B)。

## 6A. 真机 dev 联调(2026-09-23,用户点头升库之后;灰度开关保持默认关)

**环境**:共享 dev 库 `wenqu` 升 `0016 → 0017 → 0018`(`.env` 里的真实模型 Key 只注入进程环境,没复制进本 worktree);
后端 `127.0.0.1:8123` 跑本分支代码,临时 vite `localhost:5273` 指过去;**8000 上别的会话的旧契约后端、5173 上它的前端全程没动**。

**先量了升库的爆炸半径**(这是当时用户最关心的一句"会影响什么"):0018 之后,仍停在旧契约的 :8000 后端
**只有 `/api-keys` 一条路由 500**(它 `SELECT` 了已删的列),其余路由 200 ⇒ 影响面精确落在"API Key 管理页 + Key 鉴权",
不是全站瘫。

| # | 断言(真实 dev 数据 + 真实 embedding 调用) | 实测 |
| --- | --- | --- |
| 1 | 档位 `all_kbs=false` 且没勾库 → 检索 403 `API_KEY_SCOPE_DENIED` + `reason=empty_scope`(**不是**"空=全部") | ✅ |
| 2 | 改档位即时作用于其下已签发的 Key:`all_kbs` 从 false 改 true 后,同一把 Key 从"2 个库"变"全部库" | ✅ 无需重发明文 |
| 3 | 请求带 2 个库、其中 1 个在范围内 → 200 并**收窄成那 1 个**(走到真实向量化) | ✅ |
| 4 | 请求全部在范围外 → 403 `out_of_scope`,**不静默降级** | ✅ |
| 5 | A 空间的 Key 打 B 空间 → 403 `API_KEY_SPACE_MISMATCH`(`space_mismatch`) | ✅ N2 越权闸 |
| 6 | 档位挂着 Key / 成员时删档 → 400 且 `details` 带 `key_count` / `member_count` | ✅(并暴露 §6 第 6 条) |
| 7 | 解除成员挂载后删档 → 204;分配 + 设当前档(`PUT /permission-groups/{id}/current`)链路通 | ✅ 换档取代 activate |
| 8 | 灰度关时:没有任何档位的真人照样能问答(200) | ✅ 与今天逐字节一致 |
| 9 | 6 个新审计动作(`permission_group.created/updated/deleted/member_assigned/member_unassigned/current_set`)都进表,越权拒绝另有 `access.denied` | ✅ |
| 10 | 前端 4 个页面在真实 dev 数据上渲染:档位页(3 档、范围三种状态含"1 个知识库"数量兜底、成员抽屉"分配 ≠ 生效")、Key 页(所属档位/归因成员/能力与库范围均来自档位、无 `is_active` 残留 UI)、问答页(库范围为空时不误报"未分配档位")、审计页(6 个动作有中文 label) | ✅ 浏览器实测 |

**收尾时又钉死两条**(今天重跑的 5 条断言,同一套 dev 数据):Key 打档位端点 = `401 AUTH_REQUIRED`、
Key 打 Key 管理端点 = `401 AUTH_REQUIRED`(订正 §6 第 2 条),Key 打"已登记但档位缺能力"的路由 =
`403 API_KEY_CAPABILITY_DENIED` 且 `details` 里是 `capability` 而**没有** `reason`;吊销 204 后删档仍
`400 key_count=1`(坐实 §6 第 6 条)。5/5 通过。

**数据自查**:探针空间/档位/Key/成员/用户已全部删除,dev 库回到 `97 users / 21 api_keys / 21 groups / 21 mounts(6 current)`、
`alembic_version=0018`,无孤儿挂载、无人多张当前档。**回退件**:升 0018 前的 `pg_dump`
`%TEMP%/wenqu_dev_backup/wenqu_dev_pre0018_20260923_101412.dump`(522 KB)保留,`pg_restore` 即可回到 0016 形态。

## 7. 复现方式(临时件已全部清掉)

## 7. 复现方式(临时件已全部清掉)

```bash
# —— F6 那轮(临时库)——
# 临时库(跑完 DROP DATABASE wenqu_opt41_f6)
cd server && APP_ENV=dev \
  APP_DATABASE_URL='postgresql+psycopg://wenqu:wenqu_dev_only@localhost:5432/wenqu_opt41_f6' \
  APP_JWT_SECRET=... APP_MASTER_KEY=... uv run alembic upgrade head
# 后端换端口,第二轮加 APP_REQUIRE_PERMISSION_GROUP_FOR_CORE=1
uv run uvicorn app.main:app --port 8123
# 前端:临时 vite 配置 merge 基础配置,端口 5273,代理指 8123(该文件未提交)
npx vite --config vite.f6.config.mts
```

```bash
# —— §6A 那轮(共享 dev 库,已升 0018)——唯一区别:不覆盖 APP_DATABASE_URL,直接吃主仓 dev .env
#    密钥只进当前进程环境,不复制 .env 进本 worktree(所以 worktree 里没有 .env,git 也看不到)
cd /g/my/my_file/opt41-backend/server
set -a && . /g/my/my_file/my_rag/server/.env && set +a
uv run uvicorn app.main:app --port 8123          # 探针脚本同 §6A,跑完即删(TEMP 里不留文件)
docker exec my_rag-postgres-1 psql -U wenqu -d wenqu -c "select version_num from alembic_version;"  # = 0018
```

门禁:`cd web && npm run build`(= `vue-tsc -b && vite build`)EXIT=0。
