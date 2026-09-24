/**
 * 演示模式(OPT-46):登录页用的一个免登录只读接口。
 *
 * 口径(详见 docs/api/前端接入工单-OPT46演示模式.md):
 * 1. 任何时候都要能调用成功 —— 开关关着也返回 200(只是 enabled=false),前端不必分诊错误;
 * 2. enabled=true 才显示演示入口;ready=false 说明数据没播种完,提示先去跑 make demo-seed,
 *    别让观众看到半截知识库;
 * 3. 密码不在这个接口里,前端也拿不到:演示账号的密码由讲解人自己输或脚本从环境变量取。
 */
import { http } from './http'
import type { DemoStatusOut } from './types'

export function apiDemoStatus(): Promise<DemoStatusOut> {
  return http.get<DemoStatusOut>('/demo/status').then((r) => r.data)
}
