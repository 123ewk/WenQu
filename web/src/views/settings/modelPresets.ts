/**
 * 二轮§3:模型名「预设提示清单」的过渡态本地常量(按 provider type.key 索引)。
 *
 * TODO(OPT-36):后端 `GET /model-config/types` 每项将加法式下发 `model_names` 后,
 * 删掉本表、数据源切到 `t.model_names`,UI 行为不变(需求单 §3 / 后端计划 §3)。
 *
 * 口径:
 * - 这只是「常见填法提示」,不是白名单;后端不校验,自定义值必须能填能存(所以控件 filterable + allow-create)。
 * - 顺序即下拉顺序,把「最该选的」放第一个(allow-create 时首位就是默认落点)。
 * - 版本/日期后缀类名字易失更;拿不准的直接留空(宁缺毋滥,填错的表现是上游报错而用户以为系统坏了)。
 */
export interface ModelNamePreset {
  /** 预设候选模型名,按下拉顺序排列;空数组 = 无预设,渲染成纯输入框 */
  names: string[]
  /** 随该类型显示在字段下方的一次性提醒(可选) */
  note?: string
}

/** key 与后端 PROVIDER_TYPES 的 type key 一一对应(space_models.py:83-119) */
export const MODEL_NAME_PRESETS: Record<string, ModelNamePreset> = {
  deepseek: { names: ['deepseek-chat', 'deepseek-reasoner'] },
  dashscope: { names: ['qwen-plus', 'qwen-max', 'qwen-turbo'] },
  zhipu: { names: ['glm-4-plus', 'glm-4-flash'] },
  moonshot: { names: ['kimi-k2-0711-preview', 'moonshot-v1-8k'] },
  // 豆包 model_name 是方舟「接入点 ID」形态(ep-xxxx 或带日期后缀),完全按开通而定 → 无预设,让用户填
  doubao: { names: [] },
  minimax: { names: ['MiniMax-Text-01'] },
  openai_compatible: { names: ['gpt-4o-mini', 'gpt-4o'] },
  ollama: { names: ['qwen2.5:7b', 'llama3.1:8b'] },
  // vLLM 部署方自定义,无通用名可提示
  vllm: { names: [] },
  dashscope_rerank: {
    names: ['gte-rerank-v2', 'qwen3-rerank'],
    note: '百炼重排只能填 gte-rerank-v2 / qwen3-rerank;gte-rerank(无 -v2)已于 2026-05-30 下线,填它上游必报错',
  },
  siliconflow_rerank: { names: ['BAAI/bge-reranker-v2-m3'] },
  jina_rerank: { names: ['jina-reranker-v2-base-multilingual'] },
  cohere_rerank: { names: ['rerank-v3.5', 'bge-reranker-v2-m3'] },
}
