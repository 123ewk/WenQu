# 前端接入工单:OPT-46 演示模式登录页入口(2026-09-24)

**这份文档自包含**:照它做完即可,不需要先读别的文档。字段权威源是 `docs/api/openapi.json`
(§1 已把要用的部分原文抄在下面)。

## 0. 现在能不能动手?**不用动手 —— 本工单描述的前端已随 OPT-46 分支交付**

| 交付物 | 提交 | 内容 |
|---|---|---|
| 类型 + 接口层 | `d5a0268` | `web/src/api/types.ts` 的 `DemoStatusOut`、`web/src/api/demo.ts` 的 `apiDemoStatus()` |
| 登录页演示入口 | `d5a0268` | `web/src/views/login/LoginView.vue`:`onMounted` 拉一次状态,按三态渲染 |
| 演示路径 `data-testid` | `31e9d66`、`5d5cb02` | 空间切换/新建、档位新建、Key 签发、对话页、知识库页共 30+ 个钩子(附录 A 全清单) |

门禁:`vue-tsc -b && vite build` 通过。

这份文档留着有三个用途:① **契约口径**（后面有人改登录页或改这个接口时要照 §2 的三条规则,别破）;
② **回归清单**（§5,每次动演示路径后照跑）;③ **验收状态**（§6,真机那两条还没跑）。

分支还没并 main,所以**生产/dev 现在打开登录页是看不到演示入口的** —— 这不是 bug,是那个版本没有这段代码。

## 1. 契约:一个免登录只读端点

`GET /api/v1/demo/status` —— **不需要任何凭据**,开关关着也返回 200。

```jsonc
{
  "enabled": true,          // bool,必填。APP_DEMO_MODE 开关
  "username": "demo",       // string,默认 ""。演示账号用户名
  "space_name": "演示空间",  // string,默认 ""
  "kb_name": "大模型应用工程知识库", // string,默认 ""
  "document_total": 22,     // int,默认 0。演示空间里的文档总数
  "document_ready": 22,     // int,默认 0。其中已入库完成(completed)的篇数
  "ready": true             // bool,默认 false。能不能开始演示
}
```

- 除 `enabled` 外全部有默认值,所以**开关关着时前端只需看 `enabled === false`**,不用处理缺字段;
- 没有新错误码、没有新 SSE 事件、没有新表 —— 唯一的新端点就这一个。

## 2. 三条口径（改这段代码时必须守住）

1. **任何时候都要调用成功**:开关关也返回 200(只是 `enabled=false`),前端不必分诊错误码。
   因此 `apiDemoStatus()` 外面**必须包 `try/catch`**:老版本服务没有这个端点会返回 404,
   catch 住当"没有演示模式"就行,不能让登录页因为一个装饰性接口报错。
2. **`enabled=true` 才显示入口;`ready=false` 时要显示进度并提示去跑 `make demo-seed`** ——
   别让观众看到 21/22 的半截知识库。`ready` 的判据在后端(该空间 `completed > 0` 且
   `completed + failed === total`),前端**不要自己再判一遍**,直接用。
3. **密码不在这个接口里,前端也拿不到**。演示账号的密码由讲解人自己输入,或由自动演示脚本
   从环境变量 `APP_DEMO_PASSWORD` 读。谁要是提议"顺手把密码也下发一下",这是要破的纪律。

## 3. 已实现的三态渲染

| 条件 | 显示 | 测试钩子 |
|---|---|---|
| `enabled=false` 或请求失败 | 什么都不显示,登录页与 OPT-46 之前逐像素一致 | — |
| `enabled && ready` | 「一键体验」说明(带空间名与已就绪篇数)+ 按钮「填入演示账号」(把 `username` 填进登录框并切到登录标签页,密码留空由人输) | `demo-tip`、`demo-fill` |
| `enabled && !ready` | 标题「演示数据未就绪」+「演示空间文档入库进度 `document_ready/document_total`」+「需要先在服务端执行 make demo-seed」 | `demo-not-ready`(在标题上) |

代码位置:`web/src/views/login/LoginView.vue`(`demo` ref、`onMounted`、`fillDemoAccount()`、模板里的 `.demo-tip` 块)。

## 4. 数据从哪来（前端不管,但讲解时会被问到）

登录页能看到 `ready=true`,前提是服务端跑过 `make demo-seed`:它经真接口注册 demo 账号 →
建演示空间 → 建知识库 → 上传仓库自带的 22 篇文档(`demo/docs/*.md`) → 轮询到全部入库完成 →
写 3 条跨会话记忆。所以 `document_total` 是不是 22,直接反映"这个环境的种子跑全了没"。

自动演示(Playwright 驱动真浏览器)见 `docs/设计-OPT46-演示模式.md` §5.4 与 §6。

## 5. 回归清单（动过登录页 / 演示路径之后照跑）

| # | 步骤 | 期望 |
|---|---|---|
| R1 | 服务端 `APP_DEMO_MODE=0`,打开 `/login` | 无演示块;控制台无红色报错 |
| R2 | `APP_DEMO_MODE=1` 但没跑 seed | 显示"还没就绪"块,进度 `0/0` 或 `N/22`,带 `make demo-seed` 提示 |
| R3 | `APP_DEMO_MODE=1` 且 seed 跑完 | 显示「一键体验」;点按钮 → 用户名进了输入框、停在登录标签页 |
| R4 | 对着一个没有这个端点的老服务(404)打开 `/login` | 页面正常可用,演示块不出现,无未捕获异常 |
| R5 | 填对密码登录 | 落地在对话页,当前空间是 `space_name` 那个 |
| R6 | `npm run build` | 通过(本仓库前端唯一门禁) |

## 6. 验收状态

- ✅ R1/R2/R4 的判据由后端测试钉住(`server/tests/test_demo_mode.py`:开关关时不回任何线索、
  未播种时的形态、`ready` 保守判据、密码绝不出现在响应里);
- ✅ R6 `vue-tsc -b && vite build` 通过;
- ⏳ **R3/R5 还没真机点过** —— 要等 OPT-46 并 main 并发布到一个 `APP_DEMO_MODE=1` 且已 `make demo-seed` 的环境。
  `make demo-run`(自动演示脚本)跑通会顺带覆盖 R3/R5,记在 §7 A8/A9。

## 7. 禁做与提醒

- **禁**:不要在共享 dev 库上批量清理 API Key(另有会话的清理工单未关)。自动演示脚本每次会**新建**
  一把名字带时间戳的 Key(`demo-e2e-<MMDDHHMM>`),这是有意的;要收口就在页面上逐把吊销,不要写批量删。
- **禁**:不要把 `demo-out/` 里的截图提交进仓库(已进 `.gitignore`)。其中"签发 API Key"那张**带着明文 Key**,
  对外分享前要先删。
- 提醒:演示密码等于半公开(界面上有用户名、路径上有入口),**不要复用任何其它账号的密码**。

---

## 附录 A：演示路径上的 `data-testid` 全清单

自动演示脚本只认这些钩子。**改这些元素的名字/层级时,`server/scripts/demo_run.py` 会静默失效**
(表现为"等元素超时"),所以改完必须跑一次 `make demo-run`。

| 页面 | 钩子 |
|---|---|
| 登录 | `login-username`、`login-password`、`login-submit`、`demo-tip`、`demo-fill`、`demo-not-ready` |
| 空间 | `space-switcher`、`space-create-open`、`space-create-name`、`space-create-submit` |
| 导航 | `nav-chat`、`nav-kb`、`nav-api-keys`、`nav-permission-groups` |
| 权限档位 | `group-create-open`、`group-name`、`group-cap-chat`、`group-cap-documents`(按能力码生成)、`group-all-kbs`、`group-submit` |
| API Key | `key-create-open`、`key-name`、`key-group`、`key-owner`、`key-submit`、`key-secret`、`key-copy`、`key-created-close` |
| 对话 | `chat-thread`、`chat-input`、`chat-send`、`chat-answer`、`chat-stop`(停止生成按钮)、`chat-example-chip` |
| 知识库 | `kb-card`、`kb-create-open`、`kb-create-open-empty`(空态按钮)、`kb-form-name`、`kb-form-submit`、`doc-upload` |

两个 Element Plus 的坑（脚本里已按这个处理,别改回去）:
- `el-switch` 的状态**只认根节点的 `is-checked` 类**,`aria-checked` 在这个组件上不可靠;
- `el-select` / `el-popover` 的面板会挂到 `<body>` 下(不在组件原位),要先点开再按
  `.el-select-dropdown__item` / `.menu-pop .pop-item` 找选项;
- 判断"这一回答完了没有"**不能用发送按钮变回可点**:`chat-send` 发出去后就一直是灰的
  (它要求输入框非空,而脚本会清空输入框)。真正的结束信号是画布上多出一条 `chat-answer`
  并且 `chat-stop`(停止生成)按钮消失 —— 两者缺一不可,所以这两个钩子都不能删。
