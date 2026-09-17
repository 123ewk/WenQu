/**
 * 顶栏全局上传进度。
 * 只跟踪「浏览器 → 服务端」的上传阶段进度(axios onUploadProgress);
 * 之后的解析/分块/向量化由后台 worker 推进,进度需在文档列表按 status 轮询查看,
 * 契约未提供跨页面的入库进度聚合接口(见 对接缺口清单)。
 */
import { defineStore } from 'pinia'

export interface UploadTask {
  id: string
  filename: string
  sizeBytes: number
  percent: number
  /** uploading:传输中;queued:已入队,等待后台解析;error:上传失败 */
  phase: 'uploading' | 'queued' | 'error'
  error?: string
  kbId: string
  kbName: string
}

const MAX_KEEP = 50

export const useUploadStore = defineStore('uploads', () => {
  const tasks = ref<UploadTask[]>([])

  /** 进行中的数量(角标) */
  const activeCount = computed(() => tasks.value.filter((t) => t.phase === 'uploading').length)
  /** 平均进度:仅统计上传中的文件,与原型「总进度」一致 */
  const overallPercent = computed(() => {
    const running = tasks.value.filter((t) => t.phase === 'uploading')
    if (!running.length) return 100
    const sum = running.reduce((acc, t) => acc + t.percent, 0)
    return Math.round(sum / running.length)
  })

  function add(task: Omit<UploadTask, 'percent' | 'phase'>): string {
    tasks.value.unshift({ ...task, percent: 0, phase: 'uploading' })
    if (tasks.value.length > MAX_KEEP) tasks.value.length = MAX_KEEP
    return task.id
  }

  function patch(id: string, changes: Partial<UploadTask>) {
    const task = tasks.value.find((t) => t.id === id)
    if (task) Object.assign(task, changes)
  }

  function remove(id: string) {
    tasks.value = tasks.value.filter((t) => t.id !== id)
  }

  function clearFinished() {
    tasks.value = tasks.value.filter((t) => t.phase === 'uploading')
  }

  function reset() {
    tasks.value = []
  }

  return { tasks, activeCount, overallPercent, add, patch, remove, clearFinished, reset }
})
