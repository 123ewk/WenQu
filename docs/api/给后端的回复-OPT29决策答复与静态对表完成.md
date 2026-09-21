# 给后端的回复:OPT-29 决策答复 + OPT-27/28 静态对表已完成(2026-09-21)

对应你们《给前端的回复-OPT27-28落地与联调门槛.md》(§3 现在能做 / §4 待答问题)。

## 1. §4 决策答复:进度条**不加**实测/估算标记

采纳你们的建议。理由:进度条是仪表,±10% 不影响"要不要新开会话"的决策;≈ 口径留给 §2 用量页那个账单口径,那里必须区分(我们已按 `estimated_tokens` 实现)。

- **请在 done 帧与 `messages` 里不要加 `context_usage_estimated`**,契约保持 `context_used_tokens` / `context_window_tokens` 两个 int|null;
- null 组合口径确认接受:两值同 null = 老消息/本轮没调模型;used 有值 + window null = 走"窗口未配置"分支;**"used null + window 有值"若出现我们按 bug 报**;`partial=true` 的 done 帧也带两值,前端已按同一分支处理;
- 兜底回答(没打模型)两值 null → 进度条整行隐藏,不会留空壳。

## 2. §3"现在就做"三项已完成(提交在 `web-l0`,未推 main)

1. **静态对表**:已按 `docs/api/openapi.json`(52 路径 / 74 方法)逐字段勘误,修正清单:
   - OPT-27(`5fa983e`):删除不存在的 providers/models GET 列表接口 → 聚合 `GET /model-config` 单读口 + 任何写后重拉;`fallback_active` 归位到 `embedding` 对象;供应商行去 kind、按 `protocol` 分块(含共享供应商语义);`/types` 字段改 `label`+`kinds` 过滤渲染;模型路径参数改表主键 `id`;PATCH Key 三态(omit=不动/空串=清除/非空=更换,清除给独立按钮+二次确认);`api_key/base_url` blur 预校验 `/^[\x21-\x7e]*$/` 内联提示(name/model_name 不套);409 `MODEL_PROVIDER_NAME_TAKEN`/`MODEL_ID_TAKEN` 表单内联;【测试连接】`model_count===0` 禁用并提示你们给的文案;测向量化 Key 无 Key 走 503 分支;
   - OPT-28(`2dba0fb`):`/token-usage/me` 改按嵌套 `{usage}` 渲染;≈ 改由 `estimated_tokens>0` 驱动(响应无 `source` 字段);`period=all` 的 `since:null` 分支;汇总表 **403 单独兜底**(角色降级)不炸整页;表底"其他/历史归属 ≈ N"= `space_total − Σmembers`;排序补同分按 `username`;§2.3 口径文案原文照抄常驻;
   - 附带:按你们开头提示框 ③,聊天页 `GET /models` 已接**切空间重拉**,chat 清单为空时选择器替换为"去模型配置页"引导链接。
2. **错误码分支**:401(凭证类型用错)/403(角色不足)文案已分开;422 直接展示后端 message 不按 code 归并;424 `network` 兜底桶照抄 message("上游返回异常(status=…)"不改写)。
3. **空态渲染**:配置页四区块"一把 Key 都没有"可渲染;prod 未配 Key 的 `key_configured=false` 红色警示与 dev `fallback_active=true` 灰色提示双分支都在,未写死环境。

以上仅 `vue-tsc -b && vite build` 编译验证。**三解锁条件(推送/重启 server+worker/alembic 0015)解除前我们不开任何联调单**;解除后按你们 §3 的 7 步顺序实测,报告按 §6 格式(不贴 .env、不贴明文 Key、数字类附 `token-usage` 原始响应体)。

## 3. 台账编号撞车提醒

你们台账 **OPT-31 = 生成阶段 SSE `error` 缺 `code`**,但本前端仓库已把 **OPT-31 登记给了"API Key 生效分配"**(`web-l0` 提交 `3565252`,对应你们 `feat/api-keys-active` 分支)。两边引用极易混淆,**建议后端台账给 SSE 那条换个编号**,或今后统一写"台账编号 31(SSE)"。前端侧已知该待办:生成阶段失败只按 message 兜底展示,不做 code 分支,现有实现兼容,不阻塞。

## 4. 等你们的东西

- OPT-29 的 29b~29e(含 §4 承诺的"契约已就绪"回复与对接说明 §3 勘误)——完成后我们才测进度条;
- §5 解锁命令的执行结果(推送确认在用户侧)——自检 `openapi paths=52` 通过后喊我们。
