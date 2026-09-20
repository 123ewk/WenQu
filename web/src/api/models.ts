import { http } from './http'
import type { ModelCatalogOut } from './types'

/**
 * 模型清单(登录即可读,全局不挂空间)。只返回已启用模型;
 * OPT-25 起数据源为库内自定义模型(由 superuser 经 /admin/model-providers 配置),
 * 形状逐字段不变;**空清单是合法状态**(新部署未配模型),选择器按空数据渲染勿当错误。
 * `id` 是提交值(AskRequest.model_id / 未来 KB 换模型),`provider` 是显示名。
 * ⚠️ 不返回"密钥是否已配置":选中后仍可能 503 MODEL_NOT_CONFIGURED。
 */
export function apiGetModelCatalog(): Promise<ModelCatalogOut> {
  return http.get<ModelCatalogOut>('/models').then((r) => r.data)
}
