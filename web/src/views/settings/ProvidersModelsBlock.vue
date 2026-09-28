<script setup lang="ts">
/**
 * OPT-27 区块②③/④的可复用形状:供应商卡片 + 模型表格 + 两套弹窗。
 * 契约(52/74)里没有单独的 providers/models 列表接口:数据由父级把聚合
 * GET /model-config 一次性拉下来后以 props 传入,本组件只做展示与写操作;
 * 任何写成功后 emit('changed') 让父级重拉聚合(设默认会顶掉旧默认、model_count
 * 变化、掩码都只有重拉才对得齐,不在本地拼)。
 * 供应商分块看后端下发的 `providers[].kinds`(OPT-35,该供应商允许挂载的类别),前端不再猜协议;
 * 模型行按 kind 过滤;类型下拉按 ProviderTypeOut.kinds 过滤(§1.4)。
 */
import { Edit, Plus, Search, Delete, Link } from '@element-plus/icons-vue'

import {
  ASCII_NO_SPACE_RE,
  apiCreateModel,
  apiCreateProvider,
  apiDeleteModel,
  apiDeleteProvider,
  apiTestModel,
  apiTestProvider,
  apiUpdateModel,
  apiUpdateProvider,
  modelConfigErrorHint,
  providerTestBlockedHint,
  type ModelKind,
} from '@/api/modelConfig'
import type {
  ProviderTypeOut,
  SpaceModelConfigOut,
  SpaceModelOut,
  SpaceModelUpdateRequest,
  SpaceProviderOut,
  SpaceProviderUpdateRequest,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{
  kind: ModelKind
  canManage: boolean
  config: SpaceModelConfigOut
  /** 供应商类型清单:父级拉一次 /model-config/types 传下来(两区块共用) */
  types: ProviderTypeOut[]
  /** 外层卡片标题已说明类别时置真,子标题不再复读 kindLabel(重排区块用) */
  hideKindLabel?: boolean
}>()
const emit = defineEmits<{ (e: 'changed'): void }>()

const auth = useAuthStore()

const TYPE_LABELS: Record<ModelKind, string> = {
  chat: '对话模型',
  rerank: '重排',
}
const kindLabel = computed(() => TYPE_LABELS[props.kind])
const titlePrefix = computed(() => (props.hideKindLabel ? '' : kindLabel.value))

/**
 * 上下文窗口(OPT-39):预设只是快捷档,任意值靠输入框直接填。
 * CONTEXT_MAX 是前端护栏 —— 后端契约里 `context_tokens` 只有 `gt=0`(无上限),
 * 若后端按 OPT-38/39 需求单 N5 落了 le,就把这里改成与契约同值。
 */
const CONTEXT_MAX = 20_000_000
const CONTEXT_PRESETS: number[] = [4096, 8192, 32768, 64000, 65536, 128000, 131072, 200000, 262144, 512000, 1048576]
/** 量级别名(比裸数字好认);没有整齐别名的档位留空 */
const CONTEXT_ALIAS: Record<number, string> = {
  4096: '4K',
  8192: '8K',
  32768: '32K',
  65536: '64K',
  128000: '≈128K',
  131072: '128K',
  262144: '256K',
  512000: '≈500K',
  1048576: '1M',
}

/** 供应商按后端下发的 `kinds`(该供应商允许挂载的类别)分块,前端不再猜协议(OPT-35);
 *  `kinds: []` = 未知协议,两个区块都不列它 */
const providers = computed(() => props.config.providers.filter((p) => p.kinds.includes(props.kind)))
const models = computed(() => props.config.models.filter((m) => m.kind === props.kind))
/** 下拉只渲染 kinds 含本区块类别的类型(§1.4:搞错 kind 后端 422) */
const typeOptions = computed(() => props.types.filter((t) => t.kinds.includes(props.kind)))
/** 二轮§2「获取 API Key」入口:取所选类型的 doc_url 原值;空串(OpenAI 兼容自建)不渲染 */
const providerTypeDocUrl = computed(() => props.types.find((t) => t.key === providerDialog.providerType)?.doc_url || '')

/* 本地类型(Ollama/vLLM)默认地址是 127.0.0.1,prod 会被 SSRF 闸门拒(§1.6) */
function typeLabel(t: ProviderTypeOut): string {
  return /^(127\.|localhost)/.test(t.default_base_url) ? `${t.label}(本地/开发环境)` : t.label
}

/** ASCII 无空白预校验(§1.3 字符口径):命中内联提示,不等 422;name/model_name 允许中文,不套这条 */
function asciiHint(v: string): string {
  return v && !ASCII_NO_SPACE_RE.test(v) ? '只能是不含空白的 ASCII 字符(中文/空格/换行会被拒绝)' : ''
}
const baseUrlHint = computed(() => asciiHint(providerDialog.baseUrl))
const apiKeyHint = computed(() => asciiHint(providerDialog.apiKey))

/* ───── 供应商弹窗 ───── */

const providerDialog = reactive({
  open: false,
  editingId: null as string | null,
  providerType: '',
  name: '',
  baseUrl: '',
  apiKey: '',
  enabled: true,
})
const savingProvider = ref(false)
const providerError = ref('')

const editingProvider = computed(
  () => (providerDialog.editingId ? providers.value.find((p) => p.id === providerDialog.editingId) ?? null : null),
)
const canSubmitProvider = computed(
  () =>
    !!providerDialog.name.trim() &&
    !baseUrlHint.value &&
    !apiKeyHint.value &&
    (providerDialog.editingId ? true : !!providerDialog.providerType) &&
    !savingProvider.value,
)

function onProviderTypeChange(key: string) {
  // 选中类型后用该类型的 default_base_url 预填,允许改(§1.3,不硬编码清单)
  const t = (props.types ?? []).find((x) => x.key === key)
  if (t && !providerDialog.editingId) providerDialog.baseUrl = t.default_base_url
}

function openCreateProvider() {
  providerDialog.editingId = null
  providerDialog.providerType = ''
  providerDialog.name = ''
  providerDialog.baseUrl = ''
  providerDialog.apiKey = ''
  providerDialog.enabled = true
  providerError.value = ''
  providerDialog.open = true
}

function openEditProvider(p: SpaceProviderOut) {
  providerDialog.editingId = p.id
  providerDialog.name = p.name
  providerDialog.baseUrl = p.base_url
  providerDialog.apiKey = ''
  providerDialog.enabled = p.enabled
  providerError.value = ''
  providerDialog.open = true
}

async function onSubmitProvider() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !canSubmitProvider.value) return
  savingProvider.value = true
  providerError.value = ''
  try {
    if (providerDialog.editingId) {
      const body: SpaceProviderUpdateRequest = {
        name: providerDialog.name.trim(),
        base_url: providerDialog.baseUrl.trim(),
        enabled: providerDialog.enabled,
      }
      // PATCH Key 语义:字段不传 = 不动(编辑留空即不动);清除走独立按钮(显式空串)
      if (providerDialog.apiKey) body.api_key = providerDialog.apiKey
      await apiUpdateProvider(spaceId, providerDialog.editingId, body)
      ElMessage.success('供应商已保存')
    } else {
      await apiCreateProvider(spaceId, {
        provider_type: providerDialog.providerType,
        name: providerDialog.name.trim(),
        // 留空 = 后端用该类型 default_base_url;空 api_key = 免密钥本地端点
        base_url: providerDialog.baseUrl.trim(),
        api_key: providerDialog.apiKey,
        enabled: providerDialog.enabled,
      })
      ElMessage.success('供应商已创建')
    }
    providerDialog.open = false
    emit('changed')
  } catch (e) {
    providerError.value = modelConfigErrorHint(e)
  } finally {
    savingProvider.value = false
  }
}

/** 清除已存 Key:PATCH 显式空串(与"字段不传=不动"是两种语义,所以单独给动作) */
async function onClearProviderKey(p: SpaceProviderOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  const confirmed = await ElMessageBox.confirm(
    `清除供应商「${p.name}」的 API Key?清除后该供应商按免密钥端点调用,大概率直接 401。`,
    '清除 API Key',
    { type: 'warning', confirmButtonText: '确认清除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiUpdateProvider(spaceId, p.id, { api_key: '' })
    ElMessage.success('API Key 已清除')
    emit('changed')
  } catch (e) {
    ElMessage.error(modelConfigErrorHint(e))
  }
}

async function onDeleteProvider(p: SpaceProviderOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  // 级联删其下模型:数量用后端的 model_count(聚合行),不是本地过滤
  const n = p.model_count
  const confirmed = await ElMessageBox.confirm(
    n
      ? `删除供应商「${p.name}」?该供应商下 ${n} 个模型将被级联删除、不可用,此操作不可恢复。`
      : `删除供应商「${p.name}」?此操作不可恢复。`,
    '删除供应商',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiDeleteProvider(spaceId, p.id)
    ElMessage.success('供应商已删除')
    emit('changed')
  } catch (e) {
    ElMessage.error(modelConfigErrorHint(e))
  }
}

async function onTestProvider(p: SpaceProviderOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId || testing[p.id]) return
  testing[p.id] = true
  try {
    await apiTestProvider(spaceId, p.id)
    ElMessage.success(`供应商「${p.name}」连接正常`)
  } catch (e) {
    ElMessage.error(`连接失败:${modelConfigErrorHint(e)}`)
  } finally {
    testing[p.id] = false
  }
}

/* ───── 模型弹窗 ───── */

const modelDialog = reactive({
  open: false,
  editingId: null as string | null,
  providerId: '',
  modelId: '',
  modelName: '',
  /** 上下文窗口:undefined = 未填(el-input-number 清空时给的就是 undefined) */
  contextNumber: undefined as number | undefined,
  enabled: true,
  isDefault: false,
})
const savingModel = ref(false)
const modelError = ref('')

/** 二轮§3(OPT-36 已落地):模型名候选 = 所选供应商对应类型的 `model_names`(提示清单非白名单,后端不校验,[] = 无预设)。
 *  供应商行不带 type key,按「类型 default_base_url == 供应商 base_url」反推(建供应商时 base_url 即类型默认值预填);
 *  自定义过 base_url 的端点反推不到 = 按无预设渲染纯输入框。 */
const selectedModelType = computed(() => {
  const p = providers.value.find((x) => x.id === modelDialog.providerId)
  if (!p) return null
  return typeOptions.value.find((t) => t.default_base_url && t.default_base_url === p.base_url) ?? null
})
const hasModelPresets = computed(() => (selectedModelType.value?.model_names.length ?? 0) > 0)
/** 编辑态当前值不在预设里也要正常显示 → unshift 进选项 */
const modelNameOptions = computed(() => {
  const names = [...(selectedModelType.value?.model_names ?? [])]
  if (modelDialog.modelName && !names.includes(modelDialog.modelName)) names.unshift(modelDialog.modelName)
  return names
})

/**
 * 上下文窗口校验(OPT-39)。chat 新建必填(缺了后端 422),rerank 不用这一字段。
 * 老写法是 el-select + allow-create:非数字串能进值,`Number()` 出 NaN 后只把【创建】静默禁用,
 * 用户看到的是"点不动"而不是"哪里不对"(照抄表格里的 `64,000` 带逗号就会踩)。
 * 现在换成 el-input-number(天然挡掉非数字)+ 显式提示。
 */
const contextTouched = ref(false)
const contextError = computed(() => {
  if (props.kind !== 'chat') return ''
  // 未触碰不飘红(新建态一打开就满屏红是噪音);触碰后必须显式说明,不再只把【创建】静默禁用
  if (!contextTouched.value) return ''
  const n = modelDialog.contextNumber
  if (n === undefined || n === null) return '上下文窗口必填:点下方预设,或直接在输入框里填'
  if (!Number.isInteger(n) || n < 1) return '上下文窗口需为不小于 1 的整数 tokens'
  if (n > CONTEXT_MAX) return `上下文窗口最大 ${CONTEXT_MAX.toLocaleString()} tokens`
  return ''
})
const contextValid = computed(() => props.kind !== 'chat' || contextError.value === '')

function pickContextPreset(n: number) {
  modelDialog.contextNumber = n
  contextTouched.value = true
}

function openCreateModel() {
  modelDialog.editingId = null
  modelDialog.providerId = providers.value[0]?.id ?? ''
  modelDialog.modelId = ''
  modelDialog.modelName = ''
  modelDialog.contextNumber = undefined
  contextTouched.value = false
  modelDialog.enabled = true
  modelDialog.isDefault = models.value.length === 0
  modelError.value = ''
  modelDialog.open = true
}

function openEditModel(m: SpaceModelOut) {
  modelDialog.editingId = m.id
  modelDialog.providerId = m.provider_id
  modelDialog.modelId = m.model_id
  modelDialog.modelName = m.model_name
  modelDialog.contextNumber = m.context_tokens ?? undefined
  contextTouched.value = false
  modelDialog.enabled = m.enabled
  modelDialog.isDefault = m.is_default
  modelError.value = ''
  modelDialog.open = true
}

const canSubmitModel = computed(
  () =>
    !!modelDialog.providerId &&
    modelDialog.modelName.trim().length > 0 &&
    contextValid.value &&
    !savingModel.value,
)

async function onSubmitModel() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !canSubmitModel.value) return
  savingModel.value = true
  modelError.value = ''
  const ctx = props.kind === 'rerank' ? undefined : modelDialog.contextNumber
  try {
    if (modelDialog.editingId) {
      // PATCH 只开放 model_name/context_tokens/enabled/is_default(provider_id/kind/model_id 锁死)
      const body: SpaceModelUpdateRequest = {
        model_name: modelDialog.modelName.trim(),
        enabled: modelDialog.enabled,
        is_default: modelDialog.isDefault,
      }
      if (props.kind === 'chat') body.context_tokens = ctx ?? null
      await apiUpdateModel(spaceId, modelDialog.editingId, body)
      ElMessage.success('模型已保存')
    } else {
      await apiCreateModel(spaceId, {
        provider_id: modelDialog.providerId,
        kind: props.kind,
        model_name: modelDialog.modelName.trim(),
        // 留空 = 后端合成 `{供应商 name}/{model_name}`
        ...(modelDialog.modelId.trim() ? { model_id: modelDialog.modelId.trim() } : {}),
        context_tokens: ctx,
        enabled: modelDialog.enabled,
        is_default: modelDialog.isDefault,
      })
      ElMessage.success('模型已创建')
    }
    modelDialog.open = false
    // 设默认会顶掉同 kind 旧默认,局部更新会对不齐 → 统一由父级重拉聚合
    emit('changed')
  } catch (e) {
    modelError.value = modelConfigErrorHint(e)
  } finally {
    savingModel.value = false
  }
}

async function onDeleteModel(m: SpaceModelOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  const confirmed = await ElMessageBox.confirm(
    `删除模型「${m.model_id}」?问答页选择该模型的程序将不可再选,此操作不可恢复。`,
    '删除模型',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiDeleteModel(spaceId, m.id)
    ElMessage.success('模型已删除')
    emit('changed')
  } catch (e) {
    ElMessage.error(modelConfigErrorHint(e))
  }
}

/** 行内启停:关掉 = 从 GET /models 清单消失且不能被设成默认 */
async function onToggleModelEnabled(m: SpaceModelOut, v: boolean) {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  try {
    await apiUpdateModel(spaceId, m.id, { enabled: v })
    emit('changed')
  } catch (e) {
    ElMessage.error(modelConfigErrorHint(e))
  }
}

/** 逐行测试按钮的 in-flight 标记(id -> true) */
const testing = reactive<Record<string, boolean>>({})

async function onTestModel(m: SpaceModelOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId || testing[m.id]) return
  testing[m.id] = true
  try {
    await apiTestModel(spaceId, m.id)
    ElMessage.success(`模型「${m.model_name}」调用正常`)
  } catch (e) {
    ElMessage.error(`测试失败:${modelConfigErrorHint(e)}`)
  } finally {
    testing[m.id] = false
  }
}
</script>

<template>
  <div class="pm-block card">
    <div class="pm-head">
      <div>
        <div class="pm-title">{{ titlePrefix }}供应商</div>
        <div class="pm-sub">自配 Base URL 与 API Key;Key 只回显掩码,编辑留空 = 不修改(清除走行内按钮)</div>
      </div>
      <el-button v-if="canManage" type="primary" @click="openCreateProvider">
        <el-icon class="btn-ic"><Plus /></el-icon>新建供应商
      </el-button>
    </div>

    <div v-if="!providers.length" class="pm-empty">
      还没有配置供应商{{ canManage ? ',点「新建供应商」开始(需先到官网创建 API Key)' : '(联系空间管理员配置)' }}
    </div>
    <div v-else class="pm-grid">
      <div v-for="p in providers" :key="p.id" class="pm-card" :class="{ 'pm-card-off': !p.enabled }">
        <div class="pm-card-head">
          <span class="pm-card-name truncate">{{ p.name }}</span>
          <span v-if="!p.enabled" class="pm-card-offtag">已停用</span>
        </div>
        <div class="pm-card-url truncate" :title="p.base_url">{{ p.base_url }}</div>
        <div class="pm-card-meta">
          <span class="mono pm-key-hint">{{ p.key_hint || '未配置 Key' }}</span>
          <span class="pm-count">{{ p.model_count }} 个模型</span>
        </div>
        <div v-if="canManage" class="pm-card-ops">
          <el-tooltip :disabled="!providerTestBlockedHint(p)" :content="providerTestBlockedHint(p)" placement="top">
            <!-- disabled 的 button 不触发 tooltip,包一层 span -->
            <span>
              <el-button
                link
                type="primary"
                :loading="testing[p.id]"
                :disabled="!!providerTestBlockedHint(p)"
                @click="onTestProvider(p)"
              >
                <el-icon class="btn-ic"><Search /></el-icon>测试连接
              </el-button>
            </span>
          </el-tooltip>
          <el-button link @click="openEditProvider(p)">
            <el-icon class="btn-ic"><Edit /></el-icon>编辑
          </el-button>
          <el-button link type="danger" @click="onDeleteProvider(p)">
            <el-icon class="btn-ic"><Delete /></el-icon>删除
          </el-button>
        </div>
      </div>
    </div>

    <div class="pm-head pm-head-models">
      <div>
        <div class="pm-title">{{ kind === 'chat' ? '对话模型' : `${titlePrefix}模型` }}</div>
        <div class="pm-sub">
          {{ kind === 'chat' ? '问答未指定模型时使用带「默认」标记的模型;上下文窗口是进度条分母' : '开启重排后检索结果按默认重排模型重排' }}
        </div>
      </div>
      <el-button v-if="canManage" type="primary" :disabled="!providers.length" @click="openCreateModel">
        <el-icon class="btn-ic"><Plus /></el-icon>新建模型
      </el-button>
    </div>

    <el-table v-if="models.length" :data="models" style="width: 100%">
      <el-table-column label="模型 ID" min-width="150">
        <template #default="{ row }">
          <span class="pm-cell-strong">{{ row.model_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="模型名" min-width="130">
        <template #default="{ row }">
          <span class="mono">{{ row.model_name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="供应商" min-width="110">
        <template #default="{ row }">
          <span class="pm-cell-dim">{{ row.provider_name }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="kind === 'chat'" label="上下文窗口" width="110">
        <template #default="{ row }">
          <span v-if="row.context_tokens != null" class="pm-cell-dim">{{ row.context_tokens.toLocaleString() }}</span>
          <span v-else class="pm-cell-warn">未配置</span>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="70">
        <template #default="{ row }">
          <el-switch
            v-if="canManage"
            :model-value="row.enabled"
            :disabled="row.is_default"
            @update:model-value="(v: any) => onToggleModelEnabled(row as SpaceModelOut, Boolean(v))"
          />
          <span v-else :class="row.enabled ? 'pm-cell-dim' : 'pm-cell-warn'">{{ row.enabled ? '是' : '否' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="默认" width="70">
        <template #default="{ row }">
          <el-tag v-if="row.is_default" size="small" type="primary">默认</el-tag>
          <span v-else class="pm-cell-dim">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" align="right">
        <template #default="{ row }">
          <template v-if="canManage">
            <el-button link type="primary" :loading="testing[row.id]" @click="onTestModel(row as SpaceModelOut)">测试</el-button>
            <el-button link @click="openEditModel(row as SpaceModelOut)">编辑</el-button>
            <el-button link type="danger" @click="onDeleteModel(row as SpaceModelOut)">删除</el-button>
          </template>
          <span v-else class="pm-cell-dim">—</span>
        </template>
      </el-table-column>
    </el-table>
    <div v-else class="pm-empty">
      {{ providers.length ? '还没有模型,点「新建模型」添加' : ' ' }}
    </div>

    <!-- 供应商弹窗 -->
    <el-dialog
      v-model="providerDialog.open"
      :title="providerDialog.editingId ? '编辑供应商' : '新建供应商'"
      width="480px"
      append-to-body
    >
      <el-form label-position="top" @submit.prevent>
        <el-form-item v-if="!providerDialog.editingId" label="供应商类型" required>
          <el-select
            v-model="providerDialog.providerType"
            placeholder="选择供应商类型"
            class="pm-full"
            @change="onProviderTypeChange"
          >
            <el-option v-for="t in typeOptions" :key="t.key" :label="typeLabel(t)" :value="t.key" />
          </el-select>
          <div class="pm-hint">协议由类型决定、建好锁死;{{ kind === 'rerank' ? '本区块只列重排类型' : '本地类型(Ollama/vLLM)默认地址仅开发环境可存' }}</div>
          <!-- 二轮§2:类型表自带 doc_url,只在新建态且非空时渲染;文案不承诺"创建 Key"(本地类型指向模型库/文档) -->
          <a v-if="providerTypeDocUrl" class="pm-doc-link" :href="providerTypeDocUrl" target="_blank" rel="noopener noreferrer">
            <el-icon class="btn-ic"><Link /></el-icon>打开官方文档 / 控制台获取 API Key ↗
          </a>
        </el-form-item>
        <el-form-item label="显示名" required>
          <el-input v-model="providerDialog.name" maxlength="64" placeholder="本空间内唯一,如 公司 DeepSeek(允许中文)" />
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="providerDialog.baseUrl" placeholder="留空 = 用该类型的默认地址;填了必须 http(s)" />
          <div v-if="baseUrlHint" class="pm-err pm-err-inline">{{ baseUrlHint }}</div>
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="providerDialog.apiKey"
            type="password"
            show-password
            :placeholder="providerDialog.editingId ? '留空 = 不修改已存 Key' : '免密钥本地端点可留空;明文只在提交这一跳,落库即加密'"
          />
          <div v-if="apiKeyHint" class="pm-err pm-err-inline">{{ apiKeyHint }}</div>
          <div v-if="providerDialog.editingId && editingProvider?.key_hint" class="pm-hint">
            当前已存:<span class="mono">{{ editingProvider.key_hint }}</span>
            <el-button link type="danger" size="small" @click="onClearProviderKey(editingProvider!)">清除 Key</el-button>
          </div>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="providerDialog.enabled">启用</el-checkbox>
          <span class="pm-hint pm-hint-inline">关掉后其模型从问答模型清单消失</span>
        </el-form-item>
      </el-form>
      <div v-if="providerError" class="pm-err">{{ providerError }}</div>
      <template #footer>
        <el-button @click="providerDialog.open = false">取消</el-button>
        <el-button type="primary" :loading="savingProvider" :disabled="!canSubmitProvider" @click="onSubmitProvider">
          {{ providerDialog.editingId ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 模型弹窗 -->
    <el-dialog
      v-model="modelDialog.open"
      :title="modelDialog.editingId ? '编辑模型' : '新建模型'"
      width="480px"
      append-to-body
    >
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="供应商" required>
          <el-select v-model="modelDialog.providerId" class="pm-full" :disabled="!!modelDialog.editingId">
            <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
          <div v-if="modelDialog.editingId" class="pm-hint">供应商/类别/模型 ID 建好即锁死;要换请删除后重建</div>
        </el-form-item>
        <el-form-item label="模型 ID">
          <el-input
            v-if="!modelDialog.editingId"
            v-model="modelDialog.modelId"
            placeholder="留空 = 自动合成 {供应商名}/{模型名}"
          />
          <el-input v-else :model-value="modelDialog.modelId" disabled />
        </el-form-item>
        <el-form-item label="模型名" required>
          <!-- 二轮§3:有预设 → 可自定义下拉(filterable+allow-create 必须允许任意值);无预设 → 纯输入框 -->
          <el-select
            v-if="hasModelPresets"
            v-model="modelDialog.modelName"
            filterable
            allow-create
            default-first-option
            placeholder="选预设或直接输入自定义名"
            class="pm-full"
          >
            <el-option v-for="n in modelNameOptions" :key="n" :label="n" :value="n" />
          </el-select>
          <el-input v-else v-model="modelDialog.modelName" placeholder="实际调上游的名字,如 deepseek-chat(允许自定义)" />
          <div class="pm-hint">
            清单只是常见提示,后端不校验,不在清单里的名字也能保存(自定义名输入后按回车确认);填错时【测试模型】会报上游
            model_name 类错误
          </div>
          <div v-if="selectedModelType?.key === 'dashscope_rerank'" class="pm-hint">
            百炼重排只能填 gte-rerank-v2 / qwen3-rerank;gte-rerank(无 -v2)已于 2026-05-30 下线,填它上游必报错
          </div>
        </el-form-item>
        <el-form-item v-if="kind === 'chat'" label="上下文窗口(tokens)" required>
          <!-- OPT-39:预设只是快捷档,任意值直接键入(旧版 el-select+allow-create 要按回车才生效,且非法值只静默禁用提交) -->
          <el-input-number
            v-model="modelDialog.contextNumber"
            class="pm-full pm-ctx-input"
            :min="1"
            :max="CONTEXT_MAX"
            :step="1024"
            controls-position="right"
            placeholder="直接输入,如 262144"
            @blur="contextTouched = true"
          />
          <div class="pm-preset-row">
            <button
              v-for="n in CONTEXT_PRESETS"
              :key="n"
              type="button"
              class="pm-preset"
              :class="{ 'pm-preset-on': modelDialog.contextNumber === n }"
              @click="pickContextPreset(n)"
            >
              {{ n.toLocaleString() }}<span v-if="CONTEXT_ALIAS[n]" class="pm-preset-alias">{{ CONTEXT_ALIAS[n] }}</span>
            </button>
          </div>
          <div class="pm-hint">
            OPT-29 上下文进度条的分母(只算占比,服务端不据此裁剪上下文);填上游模型的真实窗口,不确定时填小不填大
          </div>
          <div v-if="contextError" class="pm-err pm-err-inline">{{ contextError }}</div>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="modelDialog.enabled">启用</el-checkbox>
          <el-checkbox v-model="modelDialog.isDefault">设为默认</el-checkbox>
          <span class="pm-hint pm-hint-inline">同空间同类别仅一个默认,勾选后自动顶替现有默认</span>
        </el-form-item>
      </el-form>
      <div v-if="modelError" class="pm-err">{{ modelError }}</div>
      <template #footer>
        <el-button @click="modelDialog.open = false">取消</el-button>
        <el-button type="primary" :loading="savingModel" :disabled="!canSubmitModel" @click="onSubmitModel">
          {{ modelDialog.editingId ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.btn-ic {
  margin-right: 3px;
}
.pm-block {
  padding: 20px;
}
.pm-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.pm-head-models {
  margin-top: 26px;
}
.pm-title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}
.pm-sub {
  margin-top: 3px;
  font-size: 12px;
  color: #9ca3af;
}
.pm-empty {
  padding: 18px;
  border: 1px dashed #e5e7eb;
  border-radius: 8px;
  font-size: 13px;
  color: #9ca3af;
  text-align: center;
}
.pm-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.pm-card {
  padding: 14px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pm-card-off {
  opacity: 0.6;
}
.pm-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.pm-card-name {
  font-size: 14px;
  font-weight: 500;
  color: #111827;
  min-width: 0;
}
.pm-card-offtag {
  flex-shrink: 0;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 6px;
  background: #f3f4f6;
  color: #9ca3af;
}
.pm-card-url {
  font-size: 12px;
  color: #6b7280;
}
.pm-card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
}
.pm-key-hint {
  color: #374151;
}
.pm-count {
  color: #9ca3af;
}
.pm-card-ops {
  display: flex;
  gap: 6px;
  border-top: 1px solid #f3f4f6;
  padding-top: 6px;
  margin-top: 2px;
}
.pm-cell-strong {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}
.pm-cell-dim {
  font-size: 13px;
  color: #6b7280;
}
.pm-cell-warn {
  font-size: 12px;
  color: #d97706;
}
.pm-hint {
  font-size: 12px;
  color: #9ca3af;
  line-height: 1.5;
}
.pm-doc-link {
  display: inline-flex;
  align-items: center;
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-color-primary);
  text-decoration: none;
}
.pm-doc-link:hover {
  text-decoration: underline;
}
.pm-hint-inline {
  margin-left: 10px;
}
.pm-full {
  width: 100%;
}
/* el-input-number 默认把数字居中显示,与同弹窗其它左对齐输入框不一致,且长数字看着像占位符 */
.pm-ctx-input :deep(.el-input__inner) {
  text-align: left;
}
/* 上下文窗口预设:与输入框并存(OPT-39),点一下填值,不替代手输 */
.pm-preset-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.pm-preset {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  height: 26px;
  padding: 0 8px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  color: #374151;
  cursor: pointer;
}
.pm-preset:hover {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
}
.pm-preset-on {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}
.pm-preset-alias {
  font-size: 11px;
  color: #9ca3af;
}
.pm-preset-on .pm-preset-alias {
  color: var(--el-color-primary);
}
.pm-err {
  font-size: 13px;
  color: #ef4444;
}
.pm-err-inline {
  margin-top: 4px;
}
</style>
