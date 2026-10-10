# 安全策略(Security Policy)

## 支持的版本

安全修复只落在最新的发布 tag 上(见 [VERSION](VERSION) 与 Releases);main 分支是开发前沿,可能包含未完成改动。

## 如何报告漏洞

**请不要用公开 Issue 报告安全漏洞。**

优先用 GitHub 的私密安全建议:仓库页 **Security → Advisories → New draft security advisory**,只有维护者可见,便于跟进与联合发布修复公告。

也可以发邮件联系维护者:`2446763380@qq.com`(提交历史中的 git user 邮箱)。

报告时请尽量附上:影响面(哪个服务/端点)、复现步骤或 PoC、你评估的严重级别。我们目标在 **72 小时内**确认收到,修复节奏视严重度而定;修复发布前请你不要公开披露细节。

## 范围

- 属于本仓库的 `server/`(API 与 Agent 运行时)、`parser/`(解析服务)、`web/`(前端)、`deploy/`(部署脚本);
- 不包括:依赖组件自身的漏洞(请直接向对应上游报告,升级会随版本发布跟进)、部署者自行配置的第三方服务(Langfuse / MinIO / 数据库)。

## 部署者的安全基线

自行部署请完成 [docs/上线手册.md](docs/上线手册.md) 的 Go-Live 翻转清单——关键项:`APP_MASTER_KEY`/`APP_JWT_SECRET` 强随机、`PARSER_GRPC_TOKEN` 必配、Postgres/MinIO 默认口令必须更换、`APP_ENV=prod` fail-closed 校验。示例配置中的 `wenqu_dev_only` 等默认值**仅供开发**,生产使用即为高危配置。
