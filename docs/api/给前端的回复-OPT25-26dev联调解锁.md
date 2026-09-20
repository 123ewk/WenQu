# 给前端的回复:OPT-25/26 dev 联调已解锁(2026-09-20)

对应你们对接文档 v2.7 §11.9 末尾的"⚠️ 端到端联调受阻"(你们提到的"其它问题"一节在
`web/对接文档.md` 里未找到,后端按 §11.9 这一行定位了问题)。
**结论:阻碍已排除,dev 后端现运行新代码,07 页可以直接补测。**

## 1. 受阻原因(诊断记录)

不是代码问题,是**运行中的 dev 进程与数据库都是旧的**:

| 项 | 受阻时状态 | 证据 |
| --- | --- | --- |
| :8000 API 服务 | 旧进程(OPT-25/26 之前的代码) | `GET /api/v1/admin/model-providers` 返回 404(新代码应为 401) |
| dev 数据库 | 迁移停在 0011 | 无 `model_providers`/`custom_models` 表,`api_keys.member_user_id` 列不存在 |
| 入库 worker | 未运行 | 上传会永远停在 pending |

OPT-25 起内置模型下线,空模型库是"合法但不可用"状态(所有模型调用 503
`MODEL_NOT_CONFIGURED`),所以只重启不配模型也不够。

## 2. 后端已做的处置(均在 dev 环境,不涉及契约与代码变更)

1. 停掉旧 uvicorn 进程;`alembic upgrade head` → dev 库到 **0012**
   (两个 worktree 的 `server/.env` 指向同一个库 `localhost:5432/wenqu`,从哪份迁移等价);
2. 从新代码重启 **server(:8000)** 与 **worker**;parser gRPC 实际监听 50071,
   两进程均以 `PARSER_GRPC_ADDR=127.0.0.1:50071` 覆盖默认值(默认 50051);
3. 按《上线手册》步骤 3a 引导:注册 `dev_admin_a3236` 并置 `is_superuser`(直接改库,dev 无引导 API),
   再经 `/api/v1/admin/*` 配置:
   - 供应商 `deepseek`(openai_compatible)+ `dashscope`(openai_compatible),Key 取自
     `server/.env` 的 `DEEPSEEK_API_KEY`/`DASHSCOPE_API_KEY`,写入即加密;
   - 默认 chat:`deepseek/deepseek-chat`;默认 embedding:`dashscope/text-embedding-v3`(dims 1024);
   - `GET /api/v1/models` 现为:`defaults = {chat: deepseek/deepseek-chat, embedding: dashscope/text-embedding-v3}`,
     rerank 仍为空清单(合法状态,你们的选择器空数据分支不受影响)。
4. 真链路冒烟(go_live_smoke,不打桩)对 dev 复跑:**25/25 通过**,其中与你们本轮接入直接相关的:
   - 条1 四格式上传 → 真 worker 全部 completed(含真向量化);
   - 条3 SSE 问答(真模型)引用可回链;条4 断流续播不重不丢;
   - 条6 "Viewer 建 Key 403(OPT-26 Admin+ 门槛)"。

## 3. 对前端的影响

- **契约零变化**:仍按 v2.7 §9.9/§9.10/§11.9 的接入结果执行,你们无需改任何代码;
- 07 页(API Key)实测的全部前置已就绪:成员下拉数据源、必填 `member_user_id`、
  Admin+ 显隐、绑定成员离开后置灰;
- 模型选择器从"空清单"变为有 1 个 chat 模型,可顺带补测"非空清单"分支;
- 上传/入库链路恢复(worker 在跑),07 页实测如恰逢 pending 状态属正常异步,轮询进度即可。

## 4. 需要前端做的

1. 补一轮 07 页端到端实测(§11.9 登记的受阻项),并把结果登记进你们文档下一版;
2. 若再遇到"接口 404 / 模型 503 / 上传永停 pending"这类现象,先对照 §1 的三条证据自查
   dev 进程与迁移版本,再在文档里上报(请写清楚复现请求与响应码)。

## 5. dev 环境手工说明(防止再次踩坑)

当前的 server/worker 是本轮以后台方式手工启动的(工作目录 `wenqu-backend/server`)。
如你们需要自己重启,先确认没有其他 uvicorn 占着 8000(用
`netstat -ano | findstr :8000` 查 PID),然后:

```bash
cd wenqu-backend/server
PARSER_GRPC_ADDR=127.0.0.1:50071 .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
PARSER_GRPC_ADDR=127.0.0.1:50071 .venv/Scripts/python.exe -m app.worker
```
