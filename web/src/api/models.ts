import { http } from './http'
import type { ModelCatalogOut } from './types'

/**
 * 模型清单(登录即可读)。OPT-27 起形状逐字段不变,但**内容 = 当前活动空间的合并快照**
 * (空间行遮蔽同名全局行;prod 未配向量化 Key 的空间 embedding 为空数组)→ 切空间后必须重拉。
 * 只返回已启用模型;空清单是合法状态(该空间与全局都没有可用模型),按空渲染并引导去配置页。
 * `id` 是提交值(AskRequest.model_id),`provider` 是显示名。
 * ⚠️ 不返回"密钥是否已配置":选中后仍可能 503 MODEL_NOT_CONFIGURED。
 */
export function apiGetModelCatalog(): Promise<ModelCatalogOut> {
  return http.get<ModelCatalogOut>('/models').then((r) => r.data)
}
