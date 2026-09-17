/**
 * 外壳层的少量动态展示状态:顶栏描述可由当前页面覆盖
 * (如对话页要显示当前回答模型,而不是路由里的静态描述)。
 */
import { defineStore } from 'pinia'

export const useUiStore = defineStore('ui', () => {
  const topbarDesc = ref<string | null>(null)

  function setTopbarDesc(desc: string | null) {
    topbarDesc.value = desc
  }

  return { topbarDesc, setTopbarDesc }
})
