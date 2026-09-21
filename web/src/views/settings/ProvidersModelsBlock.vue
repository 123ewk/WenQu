<script setup lang="ts">
/**
 * OPT-27 区块②③/④的可复用形状:供应商卡片 + 模型表格 + 两套弹窗。
 * 契约(52/74)里没有单独的 providers/models 列表接口:数据由父级把聚合
 * GET /model-config 一次性拉下来后以 props 传入,本组件只做展示与写操作;
 * 任何写成功后 emit('changed') 让父级重拉聚合(设默认会顶掉旧默认、model_count
 * 变化、掩码都只有重拉才对得齐,不在本地拼)。
 * 供应商行没有 kind,按 protocol 分块:重排协议一律以 `_rerank` 结尾,其余归对话区块
 * (dashscope_rerank 等新协议加入后不能只认 cohere_rerank);
 * 模型行按 kind 过滤;类型下拉按 ProviderTypeOut.kinds 过滤(§1.4)。
 */
import { Edit, Plus, Search, Delete } from '@element-plus/icons-vue'

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

/** §1.6 快捷预置值(前端写死即可,后端不拦);允许直接输入其他值 */
const CONTEXT_PRESETS = [4096, 32768, 65536, 128000]

/** 供应商按 protocol 分块:重排协议统一以 `_rerank` 结尾(后端从协议注册表派生)。
 *  TODO(OPT-35): view.providers[] 将加法式下发 `kinds`,届时改 `p.kinds.includes(props.kind)`,删这个后缀判断 */
const RERANK_PROTOCOL_SUFFIX = '_rerank'
const providers = computed(() =>
  props.config.providers.filter((p) =>
    props.kind === 'rerank' ? p.protocol.endsWith(RERANK_PROTOCOL_SUFFIX) : !p.protocol.endsWith(RERANK_PROTOCOL_SUFFIX),
  ),
)
const models = computed(() => props.config.models.filter((m) => m.kind === props.kind))
/** 下拉只渲染 kinds 含本区块类别的类型(§1.4:搞错 kind 后端 422) */
const typeOptions = computed(() => props.types.filter((t) => t.kinds.includes(props.kind)))

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
  contextTokens: '' as string,
  enabled: true,
  isDefault: false,
})
const savingModel = ref(false)
const modelError = ref('')

/** 上下文窗口:chat 新建必填(缺了 422),rerank 不需要;allow-create 允许直接输入 */
const contextSelectValue = computed({
  get: () => modelDialog.contextTokens,
  set: (v: string | number) => (modelDialog.contextTokens = String(v ?? '')),
})

function openCreateModel() {
  modelDialog.editingId = null
  modelDialog.providerId = providers.value[0]?.id ?? ''
  modelDialog.modelId = ''
  modelDialog.modelName = ''
  modelDialog.contextTokens = ''
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
  modelDialog.contextTokens = m.context_tokens != null ? String(m.context_tokens) : ''
  modelDialog.enabled = m.enabled
  modelDialog.isDefault = m.is_default
  modelError.value = ''
  modelDialog.open = true
}

const parsedContext = computed(() => {
  const n = Number(modelDialog.contextTokens)
  return Number.isInteger(n) && n > 0 ? n : null
})
const canSubmitModel = computed(
  () =>
    !!modelDialog.providerId &&
    modelDialog.modelName.trim().length > 0 &&
    (props.kind === 'rerank' || parsedContext.value !== null) &&
    !savingModel.value,
)

async function onSubmitModel() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !canSubmitModel.value) return
  savingModel.value = true
  modelError.value = ''
  const ctx = props.kind === 'rerank' ? undefined : (parsedContext.value ?? undefined)
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
          <el-input v-model="modelDialog.modelName" placeholder="实际调上游的名字,如 deepseek-chat(允许中文)" />
        </el-form-item>
        <el-form-item v-if="kind === 'chat'" label="上下文窗口(tokens)" required>
          <el-select
            v-model="contextSelectValue"
            filterable
            allow-create
            default-first-option
            placeholder="选常用值或直接输入"
            class="pm-full"
          >
            <el-option v-for="n in CONTEXT_PRESETS" :key="n" :label="n.toLocaleString()" :value="String(n)" />
          </el-select>
          <div class="pm-hint">OPT-29 上下文进度条的分母,必填;常用值 4096 / 32768 / 65536 / 128000</div>
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
.pm-hint-inline {
  margin-left: 10px;
}
.pm-full {
  width: 100%;
}
.pm-err {
  font-size: 13px;
  color: #ef4444;
}
.pm-err-inline {
  margin-top: 4px;
}
</style>
