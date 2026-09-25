/**
 * OPT-47 产品导览状态机。
 *
 * 为什么做在前端而不是脚本里:演示的前提是"面试官自己点开浏览器也能被带着走一遍",
 * 脚本注入的高亮层只有我这台电脑跑着才存在。
 * 导览只读不写:不改数据、不填 Key,唯一会产生的写入是"提问"这一步真的问了 3 条。
 */
import { defineStore } from 'pinia'

import router from '@/router'
import { useAuthStore } from './auth'
import { TOUR_QUESTIONS, TOUR_STEPS, type TourContext } from '@/components/tour/steps'
import type { DemoStatusOut } from '@/api/types'

export const useTourStore = defineStore('tour', () => {
  const auth = useAuthStore()

  /** 登录成功但还没点"开始导览"(知情确认弹窗) */
  const consentPending = ref(false)
  const running = ref(false)
  const index = ref(0)
  const demo = ref<DemoStatusOut | null>(null)
  /** 该步的锚点没找到(页面结构变了/还没渲染完):卡片居中显示并提示,绝不卡住流程 */
  const anchorMissing = ref(false)
  const askTotal = ref(0)
  const askDone = ref(0)
  /** 交给对话页执行的一串问题;null = 不在提问 */
  const askQueue = ref<string[] | null>(null)
  /** 提问这步被跳过时的实话(比如本空间没配对话模型),空 = 没跳过 */
  const askNote = ref('')

  const step = computed(() => TOUR_STEPS[index.value] ?? null)
  const isLast = computed(() => index.value >= TOUR_STEPS.length - 1)
  const gateOpen = computed(() => step.value?.gate === 'auto-ask' && askDone.value < askTotal.value)

  function armConsent(status: DemoStatusOut) {
    demo.value = status
    consentPending.value = true
  }

  async function enter(i: number) {
    const target = TOUR_STEPS[i]
    if (!target) return
    anchorMissing.value = false
    if (typeof target.route === 'function') {
      const ctx: TourContext = { spaceId: auth.currentSpaceId ?? '', demo: demo.value }
      try {
        await go(await target.route(ctx))
      } catch {
        await go(null) // 解析不到就留在当前页讲,别把导览卡死
      }
    } else if (target.route) {
      await go(target.route)
    }
    if (target.gate === 'auto-ask') startAsking()
  }

  async function go(path: string | null) {
    if (!path || router.currentRoute.value.fullPath === path) return
    await router.push(path)
  }

  function startAsking() {
    askTotal.value = TOUR_QUESTIONS.length
    askDone.value = 0
    askNote.value = ''
    askQueue.value = [...TOUR_QUESTIONS]
  }

  /** 对话页每答完一条回报一次;全部答完由导览自己推进 */
  function reportAskDone() {
    askDone.value += 1
    if (askDone.value >= askTotal.value) askQueue.value = null
  }

  /** 问不下去(没模型/发不出去)时由对话页回报:放开这一步,并把原因写在卡片上 */
  function reportAskSkipped(reason: string) {
    askNote.value = reason
    askDone.value = askTotal.value
    askQueue.value = null
  }

  async function start() {
    consentPending.value = false
    running.value = true
    index.value = 0
    await enter(0)
  }

  async function goto(i: number) {
    const next = Math.min(Math.max(i, 0), TOUR_STEPS.length - 1)
    index.value = next
    await enter(next)
  }

  async function next() {
    if (isLast.value) {
      stop()
      return
    }
    await goto(index.value + 1)
  }

  async function prev() {
    await goto(index.value - 1)
  }

  function stop() {
    running.value = false
    consentPending.value = false
    askQueue.value = null
    askTotal.value = 0
    askDone.value = 0
    askNote.value = ''
  }

  return {
    consentPending,
    running,
    index,
    demo,
    anchorMissing,
    askTotal,
    askDone,
    askQueue,
    askNote,
    step,
    isLast,
    gateOpen,
    armConsent,
    start,
    goto,
    next,
    prev,
    stop,
    reportAskDone,
    reportAskSkipped,
  }
})
