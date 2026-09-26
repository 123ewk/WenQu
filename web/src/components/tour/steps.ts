/**
 * OPT-47 产品导览的步骤配置。
 *
 * 导览只讲"新用户必须先知道的事",不改任何数据:模型配置那一步是**只展示不填**
 * (演示账号已经配好,现场填 Key 会把注意力带偏,还会让明文闪过输入框)。
 * 提问文案与 demo/questions.json 的 q01-q03 保持一致(那份是给外部脚本用的,前端读不到)。
 */
import { apiListDocuments, apiListKbs } from '@/api/knowledge'
import type { DemoStatusOut } from '@/api/types'

export interface TourContext {
  spaceId: string
  demo: DemoStatusOut | null
}

export interface TourStep {
  key: string
  title: string
  /** 大白话正文,一段一个 <p> */
  body: string[]
  /** 进入该步要去的页面;函数版可解析运行时 id,返回 null = 留在当前页 */
  route?: string | ((ctx: TourContext) => Promise<string | null>)
  /** 高亮控件的 data-tour 值;省略 = 居中卡片(不指任何元素) */
  anchor?: string
  /** 该步要等页面自己跑完才能继续(auto-ask = 自动提问全部答完) */
  gate?: 'auto-ask'
}

/** 导览里自动提的 3 条问题:一条链路一个问题(检索命中 / 参数取值 / 引用溯源) */
export const TOUR_QUESTIONS = [
  'RAG 的整体链路是怎么串起来的?',
  '分块大小和重叠一般怎么起步?为什么要调?',
  '嵌入模型维度选 1024 还是更高,怎么权衡?',
]

export const TOUR_STEPS: TourStep[] = [
  {
    key: 'space',
    title: '第一步：先选对空间',
    anchor: 'space-switcher',
    body: [
      '这个系统里所有东西都挂在「空间」下面：文档、模型配置、权限档位、API Key。',
      '左上角这里就是空间切换器 —— 当前用的是演示空间,里面数据是预置好的;切换空间不用重新登录。',
    ],
  },
  {
    key: 'models',
    title: '第二步：这个空间用自己的模型配置',
    route: '/settings/models',
    anchor: 'tour-models',
    body: [
      '换空间不用换账号,但要重新配一次模型 —— 每个空间各用自己的厂商 Key,互不串用。',
      '嵌入模型固定是 text-embedding-v3(1024 维),这里只填一把厂商 Key;对话模型自己挑,带「默认」标记的那条就是提问时用的。',
      '这一步只看不改:演示空间已经配好了。',
    ],
  },
  {
    key: 'groups',
    title: '第三步：权限看档位，身份看 Key',
    route: '/permission-groups',
    anchor: 'tour-groups',
    body: [
      '「权限档位」决定一次调用能做什么：能不能问答、能查哪些知识库、能不能看文档原文。',
      '「API Key」只是凭据 —— 它本身不带权限,挂在哪个档位上就有哪些权限。这样改权限不用重新发 Key。',
    ],
  },
  {
    key: 'kb',
    title: '第四步：知识库和它的文档',
    route: '/kb',
    anchor: 'tour-kb',
    body: [
      '一个空间可以有多个知识库。这里的库已经灌了 {docs} 篇大模型应用工程的笔记,状态都是「完成」。',
      '文档进来要走 上传 → 解析 → 分块 → 向量化 四步,任何一步失败都会在状态上标出来。',
    ],
  },
  {
    key: 'chunks',
    title: '第五步：文档被切成了什么样',
    route: async (ctx) => {
      const kbs = await apiListKbs(ctx.spaceId)
      const kb = kbs.find((k) => k.name === ctx.demo?.kb_name) ?? kbs[0]
      if (!kb) return null
      const page = await apiListDocuments(ctx.spaceId, kb.id, 20, 0)
      // 挑最大的一篇:演示要的是"看得到分块长什么样",脚本现场上传的那篇只有两三块
      const done = page.items.filter((d) => d.status === 'completed')
      const doc = done.sort((a, b) => b.size_bytes - a.size_bytes)[0] ?? page.items[0]
      return doc ? `/kb/${kb.id}/chunks/${doc.id}` : null
    },
    anchor: 'tour-chunks',
    body: [
      '检索不是把整篇文档丢给模型,而是按「分块」召回。这一页能看到每篇文档被切成的块和面包屑标题。',
      '分块大小与重叠是入库时最常被问的两个参数:块太大→召回不精准,太小→上下文断裂。',
    ],
  },
  {
    key: 'ask',
    title: '第六步：提问，并且看它凭什么这么答',
    route: '/chat',
    anchor: 'tour-chat',
    gate: 'auto-ask',
    body: [
      '接下来自动提 {questions} 条问题。每条回答下面能展开引用:命中了哪篇文档的哪个块、相似度多少。',
      '答不上来会明确拒答而不是编 —— 这是这套系统最在意的部分。',
    ],
  },
  {
    key: 'usage',
    title: '第七步：这次问答花了多少 token，记在谁头上',
    route: '/settings/usage',
    anchor: 'tour-usage',
    body: [
      '每次对话和每次向量化都会记 token 用量。这一页是这个空间的合计,以及按成员摊开的明细:谁调了多少输入、多少输出。',
      '刚才那 {questions} 条问题的消耗就落在这里 —— 成本归因到成员,才知道钱该算给谁。',
    ],
  },
  {
    key: 'done',
    title: '导览结束，剩下你自己点',
    body: [
      '刚才没演但都在的：输入框右侧的 Agent 开关(会先调工具再回答)、回答下方的记忆提议卡与上下文进度条、',
      '顶栏的入库进度、以及「API Keys」页 —— 那是给第三方程序调用用的凭据,跟网页登录是两条路。',
      '随便点,数据都是预置的,不会弄坏任何东西。',
    ],
  },
]
