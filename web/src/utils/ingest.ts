import type { DocumentStatus } from '@/api/types'

/** 入库状态机展示:排队 → 解析 → 分块 → 向量化 → 完成 */
export const INGEST_STEPS = ['排队', '解析', '分块', '向量化', '完成'] as const

const INDEX: Record<DocumentStatus, number> = {
  pending: 0,
  parsing: 1,
  chunking: 2,
  embedding: 3,
  completed: 4,
  failed: -1,
}

export function isTerminal(status: string): boolean {
  return status === 'completed' || status === 'failed'
}

/** 当前处于第几步(0 基);failed 返回 -1 */
export function stepIndex(status: string): number {
  return INDEX[status as DocumentStatus] ?? -1
}

/** 状态 → 中文;入参放宽为 string,未映射值(后端新增状态)原样展示 */
export function statusLabel(status: string): string {
  if (status === 'failed') return '失败'
  return INGEST_STEPS[stepIndex(status)] ?? status
}

/** 单个步骤点的状态:已完成全部点亮;进行中当前点高亮;其余灰 */
export function stepState(status: string, index: number): 'done' | 'current' | 'idle' {
  const current = stepIndex(status)
  if (status === 'completed') return 'done'
  if (current < 0) return 'idle'
  if (index < current) return 'done'
  if (index === current) return 'current'
  return 'idle'
}

/**
 * 后端仅提供 error_code(入库失败固定为 INGEST_FAILED),不提供人话失败原因,
 * 这里按 M2 接口文档约定的文案展示。
 */
export function failureReason(errorCode: string | null | undefined): string {
  if (errorCode === 'INGEST_FAILED') {
    return '解析失败:请检查文件是否加密、损坏或超出大小限制,处理后重新上传。'
  }
  return errorCode ? `处理失败:${errorCode}` : '处理失败,请重新上传。'
}

/** 允许上传的扩展名(旧版 .doc/.xls/.ppt 不支持,需另存为新格式) */
export const UPLOAD_ACCEPT = '.pdf,.docx,.xlsx,.pptx,.md,.txt'
