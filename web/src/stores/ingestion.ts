/**
 * 空间级入库进度(顶栏浮层"入库中"部分)。
 * 轮询规则:total_active > 0 时 2.5s 一轮,归零停止;页面不可见时跳过请求但保留定时器。
 * 浏览器上传完成(入队)由调用方 refresh() 一次,浮层随即从"上传中"切到"入库中"。
 */
import { defineStore } from 'pinia'

import { apiGetIngestionProgress } from '@/api/knowledge'
import type { IngestionProgressItem, IngestionProgressOut } from '@/api/types'

const POLL_MS = 2500

export const useIngestionStore = defineStore('ingestion', () => {
  const active = ref<IngestionProgressItem[]>([])
  const counts = ref<Record<string, number>>({})
  const totalActive = ref(0)
  const hasFailure = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null
  let spaceId: string | null = null
  let fetching = false

  function apply(data: IngestionProgressOut) {
    active.value = data.active
    counts.value = data.counts
    totalActive.value = data.total_active
    hasFailure.value = data.has_failure
    if (data.total_active > 0) ensureTimer()
    else stopTimer()
  }

  function ensureTimer() {
    if (timer) return
    timer = setInterval(tick, POLL_MS)
  }

  function stopTimer() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  async function tick() {
    if (document.hidden || fetching || !spaceId) return
    fetching = true
    try {
      apply(await apiGetIngestionProgress(spaceId))
    } catch {
      /* 单轮失败静默:下一轮重试;空间切换/登出由 watcher 处理 */
    } finally {
      fetching = false
    }
  }

  /** 立即拉一次(上传入队后、切空间后调用);total_active > 0 会自动开始轮询 */
  async function refresh(id: string) {
    spaceId = id
    await tick()
  }

  /** 切空间/登出:清空状态并停表 */
  function reset() {
    stopTimer()
    spaceId = null
    active.value = []
    counts.value = {}
    totalActive.value = 0
    hasFailure.value = false
  }

  return { active, counts, totalActive, hasFailure, refresh, reset }
})
