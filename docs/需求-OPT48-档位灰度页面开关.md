# OPT-48 需求单：把「人链路必须有权位」的灰度开关做成页面可拨的运行时开关

> **这份文档只写需求、环境现状和实施清单，不含任何已写好的代码。**
> 代码一行还没动（除了台账登记那一行未提交的文档改动）。执行由另一片一片做，提示词在 §9。

---

## 0. 一句话说清要什么

`APP_REQUIRE_PERMISSION_GROUP_FOR_CORE` 这个开关今天只能**运维改环境变量 + 重启进程**才能拨动。
本需求给它加一个**页面入口**：平台管理员（superuser）在页面上勾一下就能打开/关掉，不用再碰服务器。
开关的**语义一个字都不改**，改的只是"从哪儿读这个值"。

---

## 1. 我（写这份文档的会话）现在在什么环境里

这一段是给执行 agent 的**事实基线**，不是背景介绍。所有条目都是当场实测或读代码得到的。

### 1.1 仓库与分支：六个 worktree 各在干什么，本需求的代码写在哪个

一个仓库（GitHub `123ewk/WenQu`，本地 `G:\my\my_file\my_rag` 是主检出）挂了 6 个 worktree，
**它们是同一份 git 对象库的 6 个不同目录**，各检出不同分支。实测（2026-09-26；复测命令见本表下方）：

| 目录（worktree） | 检出的分支 | HEAD | 相对 `origin/main`(`24bbd3a`) | 远端有没有这条分支 | 未提交改动 | 这条线在干什么 |
|---|---|---|---|---|---|---|
| `G:\my\my_file\my_rag` | `feat/opt45-memory-compaction` | `596ad2c` | 落后 **23**，领先 **16** | **有**（且与本地同点，已推送） | 有：`M .dockerignore`、`M .gitignore`、未跟踪 `docs/api/给后端的需求单-OPT41双凭据与ApiKey权限合并.md`、`docs/isolation-diagram.html` | OPT-45 记忆/压缩 + OPT-47 导览的**前端**线；**当前会话的工作目录就在这里** |
| `G:\my\my_file\enforce-backend` | `feat/perm-group-enforcement` | `bb66f90` | 落后 **0**，领先 **1** | **没有**（未推送、无 PR） | 无（本需求文档就是那 1 个提交） | **本需求唯一该写代码的地方** |
| `G:\my\my_file\demo-backend` | `feat/opt47-demo-tour` | `18398f6` | 落后 **0**，领先 **10** | **没有**（未推送、无 PR） | 干净 | OPT-47 产品导览（8 步）前端；**本地整套服务都跑在这个目录上**（见 §1.2） |
| `G:\my\my_file\wenqu-backend` | `main` | `e04dcfc` | 落后 **16**，领先 0 | — | 干净 | 本地 `main` 的检出，**故意没同步**，别拿它当 main 用 |
| `G:\my\my_file\opt41-backend` | `feat/api-key-permission-groups` | `aa45eb4` | 落后 45，领先 **0** | 有（已合并） | 干净 | OPT-41/44，已随 PR #9 进 main，**遗留目录** |
| `G:\my\my_file\apikeys-backend` | `feat/api-keys-active` | `f550d7d` | 落后 125，领先 **0** | 有（已合并） | 干净 | OPT-31，已并 main，**遗留目录** |

**结论三条，执行 agent 必须照此办：**

1. **本需求的代码只写在 `G:\my\my_file\enforce-backend`（分支 `feat/perm-group-enforcement`）**。
   它基于 `24bbd3a`，而 `24bbd3a` **就是 `origin/main` 的 tip**（PR #16 = v0.2.0 发布合并点），
   所以这条分支是"干净的最新 main + 本需求自己的提交"，没有任何别人的在途改动混在里面。
2. **分支目前只有 1 个提交 `bb66f90`，内容是两份文档**（`docs/需求-OPT48-档位灰度页面开关.md` 269 行 + `docs/优化台账.md` 的 OPT-42/OPT-48 行）。
   `git show --stat bb66f90` 可自证：`server/`、`web/` **零改动**。**代码一行都没写。**
3. 在 `my_rag`、`demo-backend` 里看到的东西（OPT-45 悬浮窗、OPT-47 导览、`.gitignore` 的未提交改动）**都不是本需求的产物，也不要碰**；
   那两个目录里各有 16 个、10 个未并 main 的领先提交，属另两条线，push 与 PR 由用户另行决定。

复测命令（整段可复制）：

```bash
cd /g/my/my_file/enforce-backend
git worktree list                                   # 六个工作区与各自 HEAD
git rev-parse --short origin/main                   # 应为 24bbd3a(会随合并变)
git log --oneline origin/main..HEAD                 # 本分支独有的提交
git rev-list --left-right --count origin/main...HEAD # 左=落后 右=领先
git ls-remote --heads origin feat/perm-group-enforcement  # 空输出 = 未推送
```

### 1.2 此刻在跑的进程：端口 → PID → 是哪个目录起的（实测，别猜）

用 `netstat -ano | grep LISTEN` 拿 PID，再用 `Get-CimInstance Win32_Process` 拿命令行，实测结果：

| 端口 | 进程 | 真实来源目录 | 口径 |
|---|---|---|---|
| `:8000` | `uvicorn app.main:app --host 127.0.0.1 --port 8000`（PID 16308，经 `uv.exe run` 10348 → `.venv` 10572/18212 层层起的同一个服务） | **`G:\my\my_file\demo-backend\server`** | **更正：它不是从 `my_rag` 起的**（本会话先前口述有误，以本表为准）。它服务的是 OPT-47 分支，但那条分支的 `src/` **与 main 逐字节相同**（相对 `origin/main` 只改了 `server/scripts/demo_run.py` + `web/`），所以 `:8000` 跑的后端代码 = **最新 main 的代码**。它同时是**共享调试后端**，**重启前必须先告知用户** |
| `:5173` | `vite`（PID 21096，父 `cmd /c vite` 23400） | `G:\my\my_file\demo-backend\web` | 用户在浏览器里手点的就是这个，HMR 活跃，**绝不动** |
| `:50051` | `python -m parser_service.server`（PID 18156 ← `.venv` 8232 ← `uv.exe` 11192） | `G:\my\my_file\demo-backend\parser` | 解析服务，与本需求无关 |
| （无端口） | `python -m app.worker`（PID 25532 ← `.venv` 23072 ← `uv.exe` 18148） | `G:\my\my_file\demo-backend\server` | 入库 worker，在跑 |
| `:5432` / `:9000-9001` | docker（PID 7192）→ 容器 `my_rag-postgres-1` / `my_rag-minio-1` | — | dev 库 `wenqu`，alembic head **0021**；**容器名带 `my_rag` 前缀但和 `my_rag` 那个 worktree 的代码无关**，它是 compose project 名，三个 worktree 共用这一个库 |

也就是说：**四个进程（server/worker/parser/web）全部从 `demo-backend` 起，数据库是三个 worktree 共用的 dev 库。**

**这条对本需求的直接影响**：你在 `enforce-backend` 写完 N1–N6 之后，`:8000` 上**不会自动出现你的改动**（它跑的是另一个目录的代码）。
要真机验收 A2/A5/A7，必须**先告知用户并得到确认**，再选择"把 :8000 切到 enforce-backend 重启"或"另起一个端口跑本分支"；
**不要在用户不知情的情况下重启 :8000**（它正被人用来点导览）。

**`APP_DEMO_PASSWORD` 只存在于 `:8000` 那个进程的环境里，任何 `.env` 文件都没有。**
所以要跑 `make demo-run` / `demo-seed` 得向用户要密码；本需求不需要它。

### 1.3 dev 库 `wenqu` 的数据量（决定"打开会锁多少人"）

连接方式：`docker exec -e PGPASSWORD=wenqu my_rag-postgres-1 psql -U wenqu -d wenqu -c "..."`
（**`docker exec` 一律不带 `-t`**，带 `-t` 会把 `pg_dump` 类命令的输出流搞坏；dev 库的用户名=库名都是 `wenqu`，
**prod 用的是 `postgres` 用户 + compose 里 `${POSTGRES_USER}`，两别再混**）。

- `users` 101 行，其中 `is_superuser=true` **只有 1 行**：`dev_admin_a3236`（测试夹具账号）；
- `spaces` 62 个；`permission_groups` 23 个；`permission_group_members` 20 行。
  → 也就是说**dev 库里大部分成员根本没挂档位**，这正是"打开那一刻会锁人"的现实规模。
- alembic head 实测 **0021**（`server/alembic/versions/` 最新文件是 `0021_message_context_cache.py`）⇒ 本需求的迁移号是 **0022**。

### 1.4 已实测的关键代码事实（带行号，执行时先自己复核一遍）

| 事实 | 位置 | 为什么重要 |
|---|---|---|
| 开关定义：`require_permission_group_for_core: bool = Field(False, validation_alias="APP_REQUIRE_PERMISSION_GROUP_FOR_CORE")` | `server/src/app/core/config.py:113-119` | 默认 False = 今天线上行为 |
| `get_settings()` **带 `@lru_cache`** | `server/src/app/core/config.py:148-150` | **env 值在进程生命周期内是固化的**（改 env 必须重启）。本需求新增的"从库里读"必须明确同样的缓存口径，否则每次核心请求打一次库 |
| 唯一读点：`if not get_settings().require_permission_group_for_core: return None` | `server/src/app/api/deps.py:171`（在 `_member_perm_for`，函数从 `:159` 起） | 本需求要改的就是这一行的取值来源 |
| 该函数返回 None 的两条刻意分支（开关关 / 路由不在双凭据清单） | `deps.py:159-185` 注释 | 不能顺手把"清单外路由"也带上档位，否则空间管理页会失去管理入口 |
| `route_in_dual_list(path, method)` = `resolve_route_capability(...) is not None` | `server/src/app/api/api_key_auth.py:129`、`:139`，表 `_ROUTE_CAPABILITIES`（`:61`） | 决定哪些路由真的会被这个开关影响 |
| superuser 守卫：`get_current_superuser`，403 文案是「仅平台管理员可管理模型」 | `server/src/app/api/deps.py:111-115` | 文案是**模型管理专用**；复用守卫可以，**复用文案会给出一条误导信息**（见 §5 阻塞项 B2） |
| `is_superuser` 列 `default=False, server_default="false"` | `server/src/app/domain/models.py:48` | 代码里**没有任何自助提升入口**（无端点、无脚本） |
| superuser 引导**有**文档化的运维动作 | `docs/上线手册.md` §2 第 3a 步：`docker compose exec -T postgres psql -U postgres -d wenqu -c "UPDATE users SET is_superuser=true WHERE username='<运维账号>';"`（文档标题「配置模型（OPT-25 新增硬前置…）」下面第一行） | prod 走这条；**dev/本地没有对应命令**（库名/用户/容器名都不同），见 §5 阻塞项 B1 |
| 迁移门禁硬断言 `get_heads() == ["0021"]` | `server/tests/test_migration_gate.py:18-21` | 新增 0022 **必须同片改这个断言**，否则整条测试红 |
| 契约清单 `REQUIRED_PATHS` | `server/tests/test_openapi_contract.py:19`（双向一致性断言在 `:151`、`:226`） | 新端点必须登记 + `make server-openapi` 重导 |
| 错误码集中注册 | `server/src/app/core/errors.py` | 新错误码要进这里 |
| 门禁命令 | `Makefile:34` `server-test`、`:37` `server-lint`(ruff+mypy)、`:40` `server-openapi`、`:64` `web-build` | 每片必跑 |

### 1.5 顺手实测到的、会影响决策的一条

用浏览器以演示账号 `demo`（在演示空间）真实调用 `GET /api/v1/admin/model-providers` → **403**。
即：演示账号不是 superuser，页面上一旦把入口挂在 superuser 守卫后面，**演示现场就看不到这个开关**。
这不是 bug，是 §5 B1/B2 那条阻塞项的现场证据。

---

## 2. 背景机制（大白话，讲清"为什么现在没档位也能问 AI"）

系统里有**两条完全独立的认证路**，这是用户这次提问的核心困惑点：

1. **人（网页登录）**：带 `Authorization: Bearer <JWT>`。权限来自"你在这个空间是什么角色"（Admin/Member/…）。
2. **程序（第三方调用）**：带 `X-API-Key`。Key 在 OPT-41 之后**必须**绑一个**权限档位**（`permission_groups`，
   表 `api_keys.group_id`），Key 能干什么**只由档位决定**。这条路一直都按档位收口，从来没有"没档位也能跑"。

**权限档位（permission group）** 是 OPT-41 引入的"能力的唯一真相"：一个档位 = 一组能力（能问答/能检索/能改文档…）
+ 一组可见的知识库范围。OPT-31 时代的 `api_keys.is_active` 那套语义已被 0018 删列取代。

真人这一侧要不要也按档位收口，由那个灰度开关决定（代码事实见 §1.4）：

- **关（今天、默认、prod 现状）**：人只看角色。所以"没配档位照样能问答"是**设计如此**，不是漏洞。
- **开**：人在"双凭据强制清单"内的路由（问答、检索、文档增删）上也要有**生效档位**，没有就 403
  `PERMISSION_GROUP_REQUIRED`。OPT-41 需求单 Q6 拍的是「**无档位即全禁**」，不是"无档位即全允许"。

因此"打开"这个动作是**会锁人的**：库里现在 20 条成员挂载 / 101 个用户。
这也正是它被做成 fail-closed 环境变量、而不是随便点一下的页面按钮的原因 —— 本需求要在**保留这个危险性的前提下**给它一个入口。

---

## 3. 已拍板的三条决策（用户原话在前）

| # | 决策 | 原话/依据 | 含义与代价 |
|---|---|---|---|
| D1 | **粒度 = 全局**，env 仍是兜底 | 用户选「做，全局粒度 + 前置校验（推荐）」 | 打开就是**整个实例所有空间**的人都开始被校验。页面措辞**必须写明"影响整个实例"**，否则用户会以为只影响当前空间 |
| D2 | **只有 superuser 能拨** | 同上，我在方案里写明、用户未反对；依据是 `get_current_superuser`（`deps.py:111`） | 不给普通空间 Admin：任何空间的 Admin 都能一键把全实例真人锁在外面，这不对等。代价是演示账号看不到入口（§1.5） |
| D3 | **前置校验只硬校验拨开关的人自己** | 用户原话「不不不，是正在使用的人必需要有挡位(就校验他自己)」 | 我一度设计成"全实例所有真人都得有档位才允许打开"，**被否掉**。改成：拨开关时**硬校验操作者本人**在所操作空间有没有生效档位，没有就 409 拒绝（防"一开第一个被锁死的是自己"）；**其他人缺档位只做统计展示，不拦**。运行时语义完全不变——打开后每人各自被服务端校验 |

**未拍板、需要用户回答的一项**：页面 UI 由谁实现（后端只出契约工单，还是这条线也顺手做前端）。
按既有分工「只做后端 + 前端只走 `docs/api` 工单」（演示类功能才豁免过），默认按**只出工单**执行。

→ **2026-09-26 已拍:由本条线顺手把 web UI 也做了**(页面 + 侧栏入口 + `make web-build`)。

> **D2 修订(2026-09-26,执行会话与用户确认,上方原表行保留作历史)**:
> 「仅 superuser 能拨」作废,改为 **superuser 或 任意空间 Admin+(role≥30)可拨**。
> 依据:demo 实测 `is_superuser=false`(§1.5 的 403 与此一致),但它在 4 个演示空间都是 Owner(40);
> 用户原话:「我需要的是成员身份大于等于30的就可以使用这个开关,不是只有种子账号有」。
> 判定口径经确认是**任意空间** Admin+(不限定当前空间);效果仍全局(D1 不变);
> 403 用新文案「仅空间管理员(Admin)及以上可拨动此实例级开关」,不改 `get_current_superuser` 既有文案;
> 守卫实现:`deps.get_enforcement_manager`(N4)。连带:D3 的"所操作空间"落为
> 「操作者本人所在的**每个**空间都必须有生效档,缺任一即 409 列出缺档空间」(开启时;关闭不校验)。

---

## 4. 需求边界（要什么、不要什么）

### 4.1 必须满足

1. **库里没有对应记录时，行为与今天逐字节一致**：读值优先级 = 库里有值用库里的 → 库里无行回落到 env → env 默认 False。
   （"什么都不配 = 什么都不变"是 fail-closed 的底线。）
2. **页面能读能写**：`GET` 返回当前生效值 + 值的来源（库里 / env 回落）+ 影响面统计；`PUT` 拨动并留审计。
3. **读值不能每次打库**：核心请求路径（`deps.py:171`）在每次调用都要拿到这个布尔值。`get_settings()` 已被 `@lru_cache` 固化，
   DB 值必须有自己的**进程内缓存 + TTL（默认 60s，可配）+ 写时主动失效本进程**。
   口径要写进注释：**其它 worker 最多 60s 后才跟上；开关不是权限本身，晚 60s 只会晚 60s 开始拒绝，不会造成越权。**
4. **拨动是 superuser 专属**（D2），且必须走 §4.1-3 的失效路径，否则"我拨了但页面还显示旧值"会被当成 bug 报回来。
5. **前置校验（D3）在服务层做，不在前端做**：前端提示只是体验，服务端 409 才是保证。
6. **审计留痕**：谁在什么时候把开关从 X 拨到 Y（复用 `audit_logs`，与档位其它写操作同构）。
7. **契约变更走既有规矩**：新端点登记 `REQUIRED_PATHS`、新错误码进 `errors.py`、`make server-openapi` 重导、
   `test_migration_gate.py` 断言同步到 0022。

### 4.2 明确不做

- **不做按空间/按租户粒度**。要的话是另一次语义改动（每空间一列 + 清单路由要带 space_id 判定），先有真实需求再说。
- **不做"打开前必须全员有档位"的强闸门**（D3 已否）。
- **不动 `route_in_dual_list` 的路由清单**，也不动 `_member_perm_for` 里"清单外路由只看角色"那条刻意分支。
- **不做"关掉时自动帮人补档位"**之类的便利写库。
- **不改 env 变量的名字或语义**，它仍是兜底来源。
- **不做多 worker 强一致（Redis pub/sub 之类）**，60s TTL 已足够（见 §4.1-3）。
- **不在导览/演示模式里露出这个开关**（演示账号非 superuser，§1.5）。

---

## 5. 两个必须先解决的阻塞项

### B1（真阻塞）：本地/dev 没有 superuser，页面入口无法验收

- 事实：dev 库只有 `dev_admin_a3236` 一个 superuser（测试夹具），演示账号 `demo` 打 `GET /api/v1/admin/model-providers` **实测 403**。
- `is_superuser` **没有任何自助提升入口**（`models.py:48` 默认 false，代码里无端点、无脚本）；唯一文档化的引导是
  `docs/上线手册.md` §2 第 3a 步的 **prod** compose 命令，dev 环境的容器名/库名/用户都不同，那条**照抄不能跑**。
- 两个选项（**执行 agent 不要自己选，交用户拍**）：
  - **B1-a 补一条 dev 手册命令**（推荐，不动代码）：在本需求文档/上线手册里给出 dev 版
    `docker exec -e PGPASSWORD=wenqu my_rag-postgres-1 psql -U wenqu -d wenqu -c "UPDATE users SET is_superuser=true WHERE username='demo';"`
    （**改共享 dev 库的账号标志，属用户/运维动作，agent 不擅自执行**）；
  - **B1-b 放宽守卫**给普通空间 Admin —— 与 D2 直接冲突，**默认不选**。

> **B1 消解(2026-09-26)**:D2 修订为「superuser 或任意空间 Admin+(role≥30)」后,本阻塞项自然消失
> —— demo(4 个空间 Owner)即满足新口径,验收不再依赖 superuser;dev 库未做任何改动。

### B2（文案）：`get_current_superuser` 的 403 文案是模型管理专用

- 事实：`deps.py:114` 写死「仅平台管理员可管理模型」。复用守卫会让"拨档位开关被拒"显示成"你不能管模型"。
- 修法：新增一条通用文案（例如「仅平台管理员可修改实例级开关」），**不要**去改 `get_current_superuser` 的既有文案
  （那会牵动模型管理页已确认过的提示）。要么另写一个同构守卫，要么守卫支持传入文案。

> **B2 已解(2026-09-26)**:新守卫 `deps.get_enforcement_manager` 用独立文案
> 「仅空间管理员(Admin)及以上可拨动此实例级开关」;`get_current_superuser` 原样未动。

---

## 6. 契约草案（方向，不是最终字段表）

- `GET  /api/v1/admin/permission-enforcement`
  → `{ effective: bool, source: "db"|"env_fallback", updated_at, updated_by_hint, impact: { spaces, members_total, members_without_active_group, actor_has_active_group, actor_spaces_without_group: [...] } }`
  —— `impact` 就是 D3 里"其他人缺档位只统计展示"的落点；`actor_*` 是拨开关前的自查。
- `PUT  /api/v1/admin/permission-enforcement` body `{ enabled: bool }`
  → 200 回同 GET 结构；409 `PERMISSION_ENFORCEMENT_ACTOR_HAS_NO_GROUP`（D3：操作者本人在所操作空间无生效档位）；
  403（非 superuser，文案见 B2）。
- 新错误码进 `errors.py`；两个端点进 `REQUIRED_PATHS`；`/admin/*` 必须保持**永不可被 API Key 触达**（既有 fail-closed 门禁，`docs/上线手册.md:41` 翻转项 #4 有断言）。
- 路径前缀按 `admin_models.py` 的既有风格定，**动手前先看现有 `/api/v1/admin/*` 的注册方式**再定稿命名。

---

## 7. 实施切片与每片门禁

一片一提交。**每片跑门禁的命令 = `cd server && make server-lint && make server-test`（在仓库根则 `make server-lint` / `make server-test`），改了路由再加 `make server-openapi`。**

| 片 | 内容 | 触碰 | 门禁 |
|---|---|---|---|
| **N0** | ~~台账 + 本需求文档~~ **已完成**（`bb66f90`，2026-09-26） | `docs/` | 文档片，无门禁 |
| **N1** | 迁移 **0022**：全局单行开关表（`enabled`、`updated_at`、`updated_by`） | `alembic/versions/0022_*.py` | **同片改** `tests/test_migration_gate.py:18-21` 断言为 `["0022"]` |
| **N2** | 仓储 + 读值服务：DB 优先 → env 回落 → 进程内缓存（TTL 60s + 显式失效） | `domain/`、`repositories/`、`services/` | lint + test |
| **N3** | 服务层：拨动前置校验（D3，只硬校验操作者）+ 影响面统计 + 审计 | `services/` | lint + test |
| **N4** | 路由与契约：GET/PUT + 新错误码 + openapi 重导 | `routes/admin_*.py`、`core/errors.py`、`tests/test_openapi_contract.py` | lint + test + `server-openapi` |
| **N5** | `deps.py:171` 改读新服务；双凭据回归 | `api/deps.py` | lint + test（含既有档位/Key 测试全绿） |
| **N6** | 后端测试补全：回落优先级、打开态 403、自锁守卫 409、缓存失效、跨进程 TTL 口径 | `tests/` | lint + test |
| **N7** | 前端接入工单（`docs/api/`）：字段、文案要求（"影响整个实例"）、409 与 403 的提示分别怎么写、superuser 不可见时不显示入口 | `docs/api/` | 无（若用户点头由本线做前端，再加 `make web-build`） |

---

## 8. 验收清单

| # | 项 | 判据 |
|---|---|---|
| A1 | 库里无行时行为与今天一致 | 新建空库跑 `server-test` 全绿；核心路由不新增打库 |
| A2 | 页面 PUT 后**本进程立刻生效** | 拨开后自己的下一次请求即 403/通过，不用重启 |
| A3 | env 兜底仍生效 | 库里无行 + `APP_REQUIRE_PERMISSION_GROUP_FOR_CORE=1` → 打开态；有库行以库为准 |
| A4 | 缓存口径符合 §4.1-3 | 单测钉住"TTL 内不打库、显式失效立刻打库"；文档写明其它 worker ≤60s |
| A5 | 自锁守卫 | 操作者在所操作空间无生效档位 → PUT 409，且**同一个人**能正常 GET |
| A6 | 影响面不拦人 | 其他成员缺档位时 PUT 仍可成功，`impact.members_without_active_group` 数字与库里 `SELECT` 一致 |
| A7 | 权限 | 非 superuser → 403 且文案不误导（B2）；`X-API-Key` 打 `/admin/*` → 既有 fail-closed 命中 |
| A8 | 审计 | 拨动一次在 `audit_logs` 留一条，含新旧值 |
| A9 | 门禁 | `make server-test` ≥ 基线 569 passed（只增不减）；`make server-lint` 绿；`make server-openapi` 有 diff 且 `REQUIRED_PATHS` 一致 |

---

## 9. 给执行 agent 的提示词（整段复制）

```text
你在 WenQu（多租户文档知识库 RAG 平台，Python 3.12 / FastAPI / SQLAlchemy / Postgres16+pgvector / Vue3）实现 OPT-48。

【先读，别急着写代码】
1. 需求与已实测环境：G:\my\my_file\enforce-backend\docs\需求-OPT48-档位灰度页面开关.md（§1 环境事实、§3 三条已拍板决策、§4 边界、§5 两个阻塞项、§7 切片表、§8 验收）。
2. 台账：docs\优化台账.md 的 OPT-48 行与 OPT-42 行。
3. 档位这条线的既有文档（没有"设计-OPT41"这个文件，别再找）：docs\架构设计.md 的权限/隔离章节、
   docs\api\给前端的契约变更说明-OPT41权限档位.md、docs\api\前端实现工单-OPT41权限档位.md、
   docs\api\给前端的回复-OPT41前端落地与F6实测.md；最权威的语义在代码注释里
   （server\src\app\api\deps.py 的 Caller/_member_perm_for，api_key_auth.py 的 _ROUTE_CAPABILITIES）。

【工作位置 —— 分支状态已实测，照此办事】
只在 G:\my\my_file\enforce-backend 里改。这条线的事实（以你跑出来的为准，别信这里写的哈希）：
- 目录 G:\my\my_file\enforce-backend；分支 feat/perm-group-enforcement；
- 基点 24bbd3a，而 24bbd3a 就是 origin/main 的 tip（PR #16 = v0.2.0），所以这条分支 = 最新 main + 本需求自己的提交，没有别人的在途改动；
- 领先 main 的那几个提交**全是文档**（需求单 + 台账），server/ 与 web/ 零改动 —— 即 N0 已完成、**代码一行没写**；
- 远端有没有这条分支要自己查（git ls-remote --heads origin feat/perm-group-enforcement）；写文档时是**没有**（未 push、无 PR）。要不要 push、何时开 PR 由用户决定，你不要擅自 push。
N0 已交付，**你从 N1 开始**。开工前跑：
  cd /g/my/my_file/enforce-backend && git status --short && git log --oneline origin/main..HEAD && git rev-parse --short origin/main
预期：status 干净、log 只列出文档提交、origin/main tip 若已前进则先读前进的内容再动手。
再读代码核对 §1.4 表格里的行号是否仍成立 —— 行号会漂，以你读到的为准，别照抄进代码注释。
其它 worktree（my_rag = OPT-45 前端线，demo-backend = OPT-47 导览线，两者各有十几个未并 main 的领先提交）一律不碰。

【本地服务在跑什么 —— 决定你怎么验收】
实测：:8000 uvicorn、app.worker、:50051 parser、:5173 vite **四个进程全部从 G:\my\my_file\demo-backend 起**（不是 my_rag，也不是本分支）。
后果：你在 enforce-backend 写完代码，:8000 上不会出现任何变化。要真机验收 A2/A5/A7，必须先告知用户并等确认，
再选"把 :8000 切到 enforce-backend 重启"或"另起一个端口（如 :8010）跑本分支"；
更稳的做法是优先用测试（真 Postgres 的集成测）覆盖，把需要重启共享后端的项目留给用户点头后的专门一轮。
:5173 绝对不动（用户正在浏览器里点导览）。dev 库 wenqu 是三个 worktree 共用的（容器名 my_rag-postgres-1 只是 compose project 名），
迁移 0022 **不要在共享 dev 库上随手 upgrade**，先备份、先问用户。

【目标】把 APP_REQUIRE_PERMISSION_GROUP_FOR_CORE 从"只能改环境变量+重启"升级成 superuser 页面可拨的运行时开关。
开关语义一个字都不改，只改取值来源：DB 有行用 DB → 无行回落 env → env 默认 False（库里什么都不配 = 行为与今天逐字节一致）。
切片顺序（每片一个提交，逐文件 git add）：
- N1 迁移 0022：全局单行开关表（enabled + updated_at + updated_by），只加不删；同片改 test_migration_gate.py 的 head 断言为 ["0022"]。
- N2 仓储 + 读值服务：DB 有行→用 DB；无行→回落 env；进程内缓存（TTL 默认 60s，可配）+ 写时主动失效本进程，注释里写清"其它 worker ≤60s 跟上；开关不是权限本身，晚 60s 只是晚 60s 开始拒绝，不造成越权"。
- N3 服务层：拨动前置校验（只硬校验操作者本人）+ 影响面统计（成员总数/无生效档人数/本人在哪些空间无档）+ 写 audit_logs。
- N4 路由与契约：GET/PUT 两个端点（路径前缀照 server/src/app/api/routes/admin_models.py 的既有注册风格定）+ 新错误码进 errors.py + REQUIRED_PATHS + make server-openapi。
- N5 deps.py:171 改读新服务，跑双凭据相关既有测试回归（Key 链路必须完全不受影响）。
- N6 测试补全：回落优先级、打开态 403、自锁守卫 409、缓存失效、审计留痕。
- N7 前端接入工单写进 docs/api/（字段、"影响整个实例"的措辞要求、409/403 分别怎么提示、非 superuser 时不显示入口）；
  **UI 由谁实现要先问用户**，默认只出工单不改 web 页面。

【必读的实现事实（2026-09-26 实测；行号会漂，动手前自己复核，别照抄进代码注释）】
- 开关定义 server/src/app/core/config.py:113-119；get_settings() 带 @lru_cache（config.py:148-150）
  ⇒ env 值在进程内是固化的，改 env 必须重启；单测要换值就用 get_settings.cache_clear()（server/tests/conftest.py:35-37 就是这个写法）。
- 全库唯一读点 server/src/app/api/deps.py:171（函数 _member_perm_for 从 :159 起）；同函数注释（:159-185）写明
  "返回 None 的两种情况都是刻意为之"，不要顺手把清单外路由也带上档位。
- superuser 守卫 deps.py:111-115；双凭据清单在 api_key_auth.py:61(_ROUTE_CAPABILITIES)、:129(resolve_route_capability)、:139(route_in_dual_list)。
- 表名：档位是 permission_groups / permission_group_members（domain/models.py:416、:449）；
  成员关系表叫 **memberships**（models.py:109），**没有 space_members 这张表**（写统计 SQL 时踩过）。
- 迁移：当前 head=0021（server/alembic/versions/0021_message_context_cache.py），本需求用 0022；
  server/tests/test_migration_gate.py:18-21 硬断言 get_heads()==["0021"]，必须同片改成 ["0022"]。
- 契约：新端点登记 server/tests/test_openapi_contract.py:19 的 REQUIRED_PATHS（双向一致性断言在 :151、:226）+ make server-openapi 重导；
  新错误码进 server/src/app/core/errors.py。
- 门禁：make server-lint（ruff+mypy）、make server-test、make server-openapi、make web-build（前端片才用）；
  ruff line-length=100 且 E501 把中文按 2 列宽计，中文注释/长断言最容易超标。

【测试怎么连数据库 —— 决定你敢不敢碰共享库】
- 集成测试用 **testcontainers 起临时真 Postgres**（镜像 pgvector/pgvector:pg16），见 server/tests/conftest.py:15-40：
  docker 不可用就 skip 而非失败；夹具内部自己 alembic upgrade head。
  ⇒ **N1 的 0022 会被测试自己跑起来，完全不需要升共享 dev 库**；不要在 dev 库上 alembic upgrade，除非用户明确点头并先备份。
- enforce-backend 这个 worktree **没有 server/.env**（新目录；.env 不入库）。这是正常状态，CI 同样没有 .env。
  ⇒ 禁止从别的 worktree 复制 .env 过来；门禁与测试都不依赖它。只有要真链路连 dev 库时，才把变量显式注入那一个进程的环境。
- 需要读 prod/dev 真实数据做统计口径核对时，用只读 SELECT；任何 UPDATE/DELETE 都要先问用户。

【三条已拍板决策，不得自行改动】
D1 粒度=全局，页面与契约里必须写明"影响整个实例所有空间"。
D2 只有 superuser 能拨（复用 deps.py 的 get_current_superuser 守卫），不给普通空间 Admin。
D3 前置校验只硬校验拨开关的人自己（他在所操作空间有无生效档位，没有就 409），其他人缺档位只做统计展示、不拦。
    运行时语义不变：打开后每人各自被服务端校验。服务层校验才是保证，前端提示只是体验。

【两个阻塞项先停手问用户】
B1 dev/本地没有可用 superuser（dev 库 101 用户里只有测试夹具 dev_admin_a3236 一个；demo 账号实测 403；
   is_superuser 无自助入口，上线手册 §2-3a 那条提权 SQL 是 prod compose 专用，dev 的容器/库/用户都不同，照抄跑不通）。要么补一条 dev 提权 SQL 命令（改共享 dev 库要用户点头），
   要么放宽守卫（与 D2 冲突）。选哪个由用户定，你不要自己改守卫。
B2 get_current_superuser 的 403 文案「仅平台管理员可管理模型」是模型专用，复用守卫但要给档位开关一条不误导的新文案；
   不要改既有守卫的文案。

【铁律，违反即返工】
- 一片一提交、逐文件 git add（禁 git add -A / git add .）、每片跑门禁、不动别人的改动、绝不擅自 push 或并 main。
- 每片门禁：cd G:\my\my_file\enforce-backend && make server-lint && make server-test。
  历史基线 569 passed；**以你 N1 之前先跑一次得到的数字为新基线**（main 前进过就会变），只许增不许减。
- 新迁移从 0022 起（0016/0017/0018 已用，0021 是当前 alembic head），且必须同片把
  server/tests/test_migration_gate.py:18-21 的 get_heads() 断言改成 ["0022"]。
- 新端点：登记 server/tests/test_openapi_contract.py:19 的 REQUIRED_PATHS，并跑 make server-openapi 重导 docs/api/openapi.json（契约不一致 CI 会红）。
- 新错误码必须在 server/src/app/core/errors.py 注册，别用裸字符串。
- 测试涉密钥一律显式注入 dummy 或打桩：CI 环境没有 .env，本机绿 CI 红就是这条引起的。
- .env 含真实厂商 Key：只 source 进进程环境，禁止跨 worktree 复制，禁止把值打印到输出、日志或文档。
  文档/报告只允许出现 key_hint 这类脱敏字段。
- :5173（Vite，正在服务 demo-backend）绝对不动；:8000 是共享调试后端，重启前必须先告知用户并等确认；
  不碰另一会话正在改的文件与提交。
- docker exec 一律不带 -t（带 -t 会毁掉 pg_dump 类命令的输出流）。dev 库连接：
  docker exec -e PGPASSWORD=wenqu my_rag-postgres-1 psql -U wenqu -d wenqu -c "..."
- 共享 dev 库 wenqu 上的"批量清理 API Key"是禁测项，不要执行任何破坏性 SQL；改数据前先备份。
- 临时脚本/日志放仓库外，事后删除。
- 前端按既有分工：默认只写 docs/api/ 接入工单（已完成/未完成/禁测/报告四段），不要顺手改 web 页面。

【不确定性处理】决策点没写清、契约命名拿不准、要改共享环境、要碰守卫语义 —— 停下来问，不要猜着往下做。
先复述你理解的状态与要动的清单，得到确认再写代码。

【验收清单（§8 的压缩版，逐条给证据）】
A1 库里无行时行为与今天逐字节一致（空库跑测试全绿；核心路由不新增打库）。
A2 PUT 之后本进程立刻生效，不用重启。
A3 env 兜底仍生效（无库行 + APP_REQUIRE_PERMISSION_GROUP_FOR_CORE=1 ⇒ 打开态；有库行以库为准）。
A4 缓存口径按 §4.1-3 用单测钉住（TTL 内不打库、显式失效立刻打库）。
A5 操作者本人在所操作空间无生效档位 ⇒ PUT 返回 409，而同一人 GET 正常。
A6 别人缺档位不拦：PUT 仍成功，且 impact.members_without_active_group 与库里 SELECT 数字一致。
A7 非 superuser ⇒ 403 且文案不误导；X-API-Key 打 /admin/* ⇒ 既有 fail-closed 命中（不许出现 Key 能拨的情况）。
A8 拨动一次在 audit_logs 留一条，含新旧值。
A9 门禁：make server-test ≥ 基线（只增不减）、make server-lint 绿、make server-openapi 有 diff 且契约一致。

【交付】跑完 A1–A9，逐片给出提交号 + 门禁输出结论（测试数、lint 结果、契约 diff 概要），
并说明 A2/A4 是被哪条测试或哪次实测钉住的。跑不了的项直说"未验证 + 为什么"，不要写"应该没问题"。
最后更新 docs/优化台账.md 的 OPT-48 行（状态从"未写代码"改成实际进度）。
```

---

## 10. 与既有台账项的关系

- **OPT-42** = "真的把它打开"这个动作（含先给真人配档位的硬前置）；**OPT-48** 只负责"怎么拨"。两者不合并。
- **OPT-41** 的 A–H 与 OPT-44 已随 PR #9 并入 main（`52bc6c7`），本需求是它的**运维入口改造**，不改档位数据模型（除新增一张全局开关表）。
- **OPT-46/47**（演示模式与产品导览）不在本需求范围；§5 B1 是唯一被它们牵连的地方（演示账号非 superuser）。

---

## 11. 真机验收轮（第二轮,2026-09-26 用户发起;本节先写方案再照做,结果追加在 11.3）

### 11.1 环境方案（不碰 :8000/:5173,不改别人 worktree）

| 项 | 决定 |
|---|---|
| 后端 | 另起 **:8010**,从 `enforce-backend/server` 跑本分支代码;环境变量 `set -a; . /g/my/my_file/my_rag/server/.env; set +a` **只 source 进进程**（不复制 .env、不打印值,键名清单已核对:无 APP_REQUIRE_PERMISSION_GROUP_FOR_CORE ⇒ 初始回落态=关） |
| 前端 | 另起 **:5174**,从 `enforce-backend/web` 跑 vite,`VITE_PROXY_TARGET=http://localhost:8010`（vite 代理目标已可配,提交 `6ab41dc`;后端无 CORS,必须走代理） |
| 数据库 | 共享 dev 库 wenqu,head 0021 → 本轮 **先备份 → upgrade 0022 → 测完 downgrade 0021**。降级原因:三个 worktree 共用此库,`alembic_version` 停在 0022 会让 demo-backend/my_rag 的 `alembic upgrade head` 报"Can't locate revision" |
| 备份 | `docker exec`（不带 -t）`pg_dump -U wenqu -d wenqu` 落到**仓库外** `~/wenqu-backup-pre-opt48-live-<日期>.sql`,完成后报告路径,由用户决定去留 |
| 账号 | `uitester01`（「UI 冒烟空间」「第二空间」Owner=40,联调记忆中的凭证）走管理员路径;`viewercx01eg`（Viewer）走无权路径。**不注册新用户、不用 demo 账号**（demo 密码不进任何输出） |

### 11.2 浏览器用例（DOM innerText 为证据,截图不可靠）

| # | 用例 | 期望 |
|---|---|---|
| U1 | 未登录直开 `/settings/permission-enforcement` | 重定向登录页 |
| U2 | `viewercx01eg` 登录 | 侧栏**无**「档位灰度开关」;直开该路由显示空态「仅空间管理员(Admin)及以上…」（A7 文案真机） |
| U3 | `uitester01` 登录开页 | 入口可见;开关=关、来源=环境变量兜底;impact 数字与 psql 只读 SELECT 一致;自查行提示缺档空间含其两个空间 |
| U4 | uitester01 未配档直接拨 | 确认弹窗文案含「影响整个实例」+ 无档人数;确认后 **409** 提示列出缺档空间;开关保持关;页面仍可读（A5 真机） |
| U5 | UI 里给两个空间各建档位+自挂+设当前 | 回开关页刷新,自查行变「你所在的空间都已配置生效档位,可以安全打开」 |
| U6 | 拨开 | 成功;开关=开、来源=页面配置(库里)、updated_by_hint=uitester01 |
| U7 | 切 `viewercx01eg` 开知识库页 | 403「本空间还没有为你分配权限档位…」（A2 开方向真机） |
| U8 | uitester01 拨关 → viewercx01eg 刷新 | 恢复可见（A2 关方向真机） |

### 11.3 结果（执行后填写）

- 待执行。
