<script setup lang="ts">
/**
 * OPT-47 产品导览层:知情确认弹窗 + 逐步高亮卡片。
 *
 * 两个设计约束:
 * 1. 遮罩与卡片都不吃掉点击(pointer-events:none,只有卡片本身可点)—— 讲解时观众想自己点
 *    侧栏、想点开引用都能立刻做,导览不是锁人的向导;
 * 2. 锚点找不到时卡片居中并提示,绝不卡住流程(页面结构一变就卡死是这类向导最常见的坑)。
 */
import { TOUR_QUESTIONS, TOUR_STEPS } from '@/components/tour/steps'
import { useTourStore } from '@/stores/tour'

const tour = useTourStore()
const total = TOUR_STEPS.length

const PAD = 8
const GAP = 12
const rect = ref<DOMRect | null>(null)
const cardStyle = ref<Record<string, string>>({})
const cardRef = ref<HTMLElement | null>(null)

let retryTimer: ReturnType<typeof setTimeout> | null = null

function anchorEl(name: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-tour="${name}"]`)
}

async function locate(retry = 8) {
  const step = tour.step
  if (!tour.running || !step) {
    rect.value = null
    return
  }
  if (!step.anchor) {
    rect.value = null
    tour.anchorMissing = false
    placeCard()
    return
  }
  const el = anchorEl(step.anchor)
  if (!el) {
    if (retry > 0) {
      retryTimer = setTimeout(() => void locate(retry - 1), 150)
      return
    }
    rect.value = null
    tour.anchorMissing = true
    placeCard()
    return
  }
  tour.anchorMissing = false
  rect.value = el.getBoundingClientRect()
  await nextTick()
  placeCard()
}

/** 卡片优先放在高亮框的下方,放不下就上方/居中 */
function placeCard() {
  const h = cardRef.value?.offsetHeight ?? 260
  const w = cardRef.value?.offsetWidth ?? 380
  const box = rect.value
  if (!box) {
    cardStyle.value = { left: '50%', top: '50%', transform: 'translate(-50%,-50%)' }
    return
  }
  const below = box.bottom + GAP
  const top = below + h < window.innerHeight ? below : Math.max(16, box.top - GAP - h)
  const left = Math.min(Math.max(16, box.left), Math.max(16, window.innerWidth - w - 16))
  cardStyle.value = { left: `${left}px`, top: `${top}px`, transform: 'none' }
}

const holeStyle = computed(() => {
  const b = rect.value
  if (!b) return { display: 'none' }
  return {
    left: `${b.left - PAD}px`,
    top: `${b.top - PAD}px`,
    width: `${b.width + PAD * 2}px`,
    height: `${b.height + PAD * 2}px`,
  }
})

function onScrollOrResize() {
  if (tour.running) void locate(0)
}

watch(
  () => [tour.running, tour.index, tour.step?.key],
  () => {
    if (retryTimer) clearTimeout(retryTimer)
    void locate()
  },
)

onMounted(() => {
  window.addEventListener('resize', onScrollOrResize)
  window.addEventListener('scroll', onScrollOrResize, true)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onScrollOrResize)
  window.removeEventListener('scroll', onScrollOrResize, true)
  if (retryTimer) clearTimeout(retryTimer)
})

const nextLabel = computed(() => {
  if (tour.gateOpen) return `提问中 ${tour.askDone}/${tour.askTotal}…`
  return tour.isLast ? '结束导览' : '下一步'
})

/** 文案里的 {docs}/{questions} 换成真实数字:数据和题目清单改了,导览不能还在报旧数 */
function fmt(s: string): string {
  return s
    .replace(/\{docs\}/g, String(tour.demo?.document_ready ?? 0))
    .replace(/\{questions\}/g, String(TOUR_QUESTIONS.length))
}
</script>

<template>
  <!-- 知情确认:演示账号登录后先说清"这是什么、会发生什么",再决定要不要被带着走 -->
  <el-dialog
    v-model="tour.consentPending"
    width="520px"
    :close-on-click-modal="false"
    :show-close="false"
    data-testid="tour-consent"
  >
    <template #header>
      <div class="consent-title">这是演示环境，先说明一下</div>
    </template>
    <ul class="consent-list" data-testid="tour-consent-body">
      <li>知识库里的 {{ tour.demo?.document_ready ?? 0 }} 篇笔记是预置的,你可以随便问,不会动到任何真实数据。</li>
      <li>接下来有一个 {{ TOUR_STEPS.length }} 步的自动导览,带你看完"用之前要准备什么"和一次真实问答。</li>
      <li>导览中途不锁页面,随时可以点「跳过」自己逛。</li>
    </ul>
    <template #footer>
      <div class="consent-foot">
        <button class="ghost" data-testid="tour-consent-later" @click="tour.stop()">我自己看</button>
        <button class="primary" data-testid="tour-consent-start" @click="tour.start()">开始导览</button>
      </div>
    </template>
  </el-dialog>

  <template v-if="tour.running && tour.step">
    <div class="hole" :style="holeStyle" />
    <div ref="cardRef" class="card" data-testid="tour-card" :style="cardStyle">
      <div class="head">
        <span class="idx mono" data-testid="tour-progress">{{ tour.index + 1 }}/{{ total }}</span>
        <strong data-testid="tour-title">{{ tour.step.title }}</strong>
        <button class="close" data-testid="tour-skip" @click="tour.stop()">跳过导览</button>
      </div>
      <p v-for="(line, i) in tour.step.body" :key="i" class="line">{{ fmt(line) }}</p>
      <p v-if="tour.anchorMissing" class="warn">
        这一步没能定位到界面元素(页面可能刚改过),文字照常,继续往下走。
      </p>
      <p v-if="tour.askNote" class="warn" data-testid="tour-ask-note">{{ tour.askNote }}</p>
      <div class="foot">
        <button class="ghost" :disabled="tour.index === 0" data-testid="tour-prev" @click="tour.prev()">
          上一步
        </button>
        <button class="primary" :disabled="tour.gateOpen" data-testid="tour-next" @click="tour.next()">
          {{ nextLabel }}
        </button>
      </div>
    </div>
  </template>
</template>

<style scoped>
.hole {
  position: fixed;
  z-index: 4000;
  border-radius: 10px;
  box-shadow: 0 0 0 9999px rgb(15 23 42 / 62%);
  pointer-events: none;
  transition:
    left 0.18s ease,
    top 0.18s ease,
    width 0.18s ease,
    height 0.18s ease;
}
.card {
  position: fixed;
  z-index: 4001;
  width: 380px;
  max-width: calc(100vw - 32px);
  padding: 16px 18px;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 18px 48px rgb(15 23 42 / 28%);
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.idx {
  font-size: 12px;
  color: #64748b;
}
.mono {
  font-variant-numeric: tabular-nums;
}
.head strong {
  flex: 1;
  font-size: 15px;
}
.close {
  border: 0;
  background: none;
  font-size: 12px;
  color: #94a3b8;
  cursor: pointer;
}
.line {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.7;
  color: #334155;
}
.warn {
  margin: 8px 0 0;
  font-size: 12px;
  color: #b45309;
}
.foot {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-top: 12px;
}
.ghost,
.primary {
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
}
.ghost {
  border: 1px solid #e2e8f0;
  background: #fff;
  color: #475569;
}
.ghost:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.primary {
  border: 1px solid #4f46e5;
  background: #4f46e5;
  color: #fff;
}
.primary:disabled {
  opacity: 0.6;
  cursor: progress;
}
.consent-title {
  font-size: 16px;
  font-weight: 600;
}
.consent-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
  color: #334155;
}
.consent-foot {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
