<script setup lang="ts">
/**
 * OPT-27 区块②③的可复用形状:供应商卡片列表 + 模型表格 + 两套新建/编辑弹窗。
 * 对接说明 §1.5 明确 rerank "与区块②③同款形状,复用同一套 providers/models 端点",
 * 故本组件按 kind 参数化,chat/rerank 各挂一份。
 * 权限:props.canManage=false 时整块只读(写按钮隐藏)。
 * 模型清单通过 models-change 事件回抛给父级(rerank 开关的"无默认模型"黄色提示要用)。
 */
import { Edit, Plus, RefreshRight, Search, Delete } from '@element-plus/icons-vue'

import {
  apiCreateModel,
  apiCreateProvider,
  apiDeleteModel,
  apiDeleteProvider,
  apiGetModelConfigTypes,
  apiListModels,
  apiListProviders,
  apiTestModel,
  apiTestProvider,
  apiUpdateModel,
  apiUpdateProvider,
  modelTestErrorHint,
  type ModelKind,
} from '@/api/modelConfig'
import { errMessage } from '@/api/http'
import type { ModelProviderOut, ModelProviderUpdateRequest, ModelTypeOption, SpaceModelOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ kind: ModelKind; canManage: boolean }>()
const emit = defineEmits<{ (e: 'models-change', models: SpaceModelOut[]): void }>()

const auth = useAuthStore()

const TYPE_LABELS: Record<ModelKind, string> = {
  chat: '对话模型',
  rerank: 'Rerank 重排',
}
const kindLabel = computed(() => TYPE_LABELS[props.kind])

const providers = ref<ModelProviderOut[]>([])
const models = ref<SpaceModelOut[]>([])
const types = ref<ModelTypeOption[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)
/** 逐行测试按钮的 in-flight 标记(id -> true) */
const testing = reactive<Record<string, boolean>>({})

/** §1.6 快捷预置值(前端写死即可,后端不拦);允许直接输入其他值 */
const CONTEXT_PRESETS = [4096, 32768, 65536, 128000]

function typeDisplayName(key: string): string {
  return types.value.find((t) => t.key === key)?.display_name ?? key
}

function providerModels(providerId: string): SpaceModelOut[] {
  return models.value.filter((m) => m.provider_id === providerId)
}

/* ───── 数据加载 ───── */

watch(
  () => auth.currentSpaceId,
  () => load(),
  { immediate: true },
)

async function load() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  loading.value = true
  loadError.value = null
  try {
    const [p, m] = await Promise.all([apiListProviders(spaceId, props.kind), apiListModels(spaceId, props.kind)])
    providers.value = p
    models.value = m
    emit('models-change', m)
    if (!types.value.length) types.value = await apiGetModelConfigTypes(spaceId)
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

/* ───── 供应商弹窗 ───── */

const providerDialog = reactive({
  open: false,
  editingId: null as string | null,
  providerType: '',
  name: '',
  baseUrl: '',
  apiKey: '',
})
const savingProvider = ref(false)
const providerError = ref('')

/** 新建必填 Key;编辑留空 = 不修改 */
const providerKeyRequired = computed(() => !providerDialog.editingId)
const canSubmitProvider = computed(
  () =>
    !!providerDialog.providerType &&
    providerDialog.name.trim().length > 0 &&
    providerDialog.baseUrl.trim().length > 0 &&
    (!providerKeyRequired.value || providerDialog.apiKey.trim().length > 0) &&
    !savingProvider.value,
)

function onProviderTypeChange(key: string) {
  // 选中类型后用 default_base_url 预填,允许用户改(§1.3)
  const t = types.value.find((x) => x.key === key)
  if (t && !providerDialog.editingId) providerDialog.baseUrl = t.default_base_url
}

function openCreateProvider() {
  providerDialog.editingId = null
  providerDialog.providerType = ''
  providerDialog.name = ''
  providerDialog.baseUrl = ''
  providerDialog.apiKey = ''
  providerError.value = ''
  providerDialog.open = true
}

function openEditProvider(p: ModelProviderOut) {
  providerDialog.editingId = p.id
  providerDialog.providerType = p.provider_type
  providerDialog.name = p.name
  providerDialog.baseUrl = p.base_url
  providerDialog.apiKey = ''
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
      const body: ModelProviderUpdateRequest = {
        name: providerDialog.name.trim(),
        base_url: providerDialog.baseUrl.trim(),
      }
      // 留空 = 不改 Key:字段整个不出现在 body 里
      if (providerDialog.apiKey.trim()) body.api_key = providerDialog.apiKey.trim()
      await apiUpdateProvider(spaceId, providerDialog.editingId, body)
    } else {
      await apiCreateProvider(spaceId, props.kind, {
        provider_type: providerDialog.providerType,
        name: providerDialog.name.trim(),
        base_url: providerDialog.baseUrl.trim(),
        api_key: providerDialog.apiKey.trim(),
      })
    }
    providerDialog.open = false
    await load()
  } catch (e) {
    providerError.value = errMessage(e)
  } finally {
    savingProvider.value = false
  }
}

async function onDeleteProvider(p: ModelProviderOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  const n = providerModels(p.id).length
  const confirmed = await ElMessageBox.confirm(
    n
      ? `删除供应商「${p.name}」?该供应商下 ${n} 个模型将不可用(级联删除),此操作不可恢复。`
      : `删除供应商「${p.name}」?此操作不可恢复。`,
    '删除供应商',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiDeleteProvider(spaceId, p.id)
    ElMessage.success('供应商已删除')
    await load()
  } catch (e) {
    ElMessage.error(errMessage(e))
  }
}

async function onTestProvider(p: ModelProviderOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId || testing[p.id]) return
  testing[p.id] = true
  try {
    await apiTestProvider(spaceId, p.id)
    ElMessage.success(`供应商「${p.name}」连接正常`)
  } catch (e) {
    ElMessage.error(`连接失败:${modelTestErrorHint(e)}`)
  } finally {
    testing[p.id] = false
  }
}

/* ───── 模型弹窗 ───── */

const modelDialog = reactive({
  open: false,
  editingId: null as string | null,
  providerId: '',
  modelName: '',
  contextTokens: '' as string,
  isDefault: false,
})
const savingModel = ref(false)
const modelError = ref('')

/** 上下文窗口:chat 必填(§1.4),rerank 无此字段(§1.5);allow-create 允许直接输入 */
const contextSelectValue = computed({
  get: () => modelDialog.contextTokens,
  set: (v: string | number) => (modelDialog.contextTokens = String(v ?? '')),
})

function openCreateModel() {
  modelDialog.editingId = null
  modelDialog.providerId = providers.value[0]?.id ?? ''
  modelDialog.modelName = ''
  modelDialog.contextTokens = ''
  modelDialog.isDefault = models.value.length === 0
  modelError.value = ''
  modelDialog.open = true
}

function openEditModel(m: SpaceModelOut) {
  modelDialog.editingId = m.id
  modelDialog.providerId = m.provider_id
  modelDialog.modelName = m.model_name
  modelDialog.contextTokens = m.context_tokens != null ? String(m.context_tokens) : ''
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
      await apiUpdateModel(spaceId, modelDialog.editingId, {
        model_name: modelDialog.modelName.trim(),
        context_tokens: ctx,
        is_default: modelDialog.isDefault,
      })
    } else {
      await apiCreateModel(spaceId, props.kind, {
        provider_id: modelDialog.providerId,
        model_name: modelDialog.modelName.trim(),
        context_tokens: ctx,
        is_default: modelDialog.isDefault,
      })
    }
    modelDialog.open = false
    await load()
  } catch (e) {
    modelError.value = errMessage(e)
  } finally {
    savingModel.value = false
  }
}

async function onDeleteModel(m: SpaceModelOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId) return
  const confirmed = await ElMessageBox.confirm(
    `删除模型「${m.model_name}」?问答页选择该模型的程序将不可再选,此操作不可恢复。`,
    '删除模型',
    { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
  ).catch(() => false)
  if (!confirmed) return
  try {
    await apiDeleteModel(spaceId, m.id)
    ElMessage.success('模型已删除')
    await load()
  } catch (e) {
    ElMessage.error(errMessage(e))
  }
}

async function onTestModel(m: SpaceModelOut) {
  const spaceId = auth.currentSpaceId
  if (!spaceId || testing[m.id]) return
  testing[m.id] = true
  try {
    await apiTestModel(spaceId, m.id)
    ElMessage.success(`模型「${m.model_name}」调用正常`)
  } catch (e) {
    ElMessage.error(`测试失败:${modelTestErrorHint(e)}`)
  } finally {
    testing[m.id] = false
  }
}
</script>

<template>
  <div class="pm-block card" v-loading="loading">
    <div class="pm-head">
      <div>
        <div class="pm-title">{{ kindLabel }}供应商</div>
        <div class="pm-sub">自配 Base URL 与 API Key;Key 只回显掩码,编辑留空 = 不修改</div>
      </div>
      <el-button v-if="canManage" type="primary" @click="openCreateProvider">
        <el-icon class="btn-ic"><Plus /></el-icon>新建供应商
      </el-button>
    </div>

    <div v-if="loadError" class="pm-empty">{{ loadError }}
      <el-button class="state-btn" @click="load">
        <el-icon class="btn-ic"><RefreshRight /></el-icon>重试
      </el-button>
    </div>
    <div v-else-if="!providers.length" class="pm-empty">
      还没有配置供应商{{ canManage ? ',点「新建供应商」开始(需先到官网创建 API Key)' : '(联系空间管理员配置)' }}
    </div>
    <div v-else class="pm-grid">
      <div v-for="p in providers" :key="p.id" class="pm-card">
        <div class="pm-card-head">
          <span class="pm-card-name truncate">{{ p.name }}</span>
          <span class="pm-card-type">{{ typeDisplayName(p.provider_type) }}</span>
        </div>
        <div class="pm-card-url truncate" :title="p.base_url">{{ p.base_url }}</div>
        <div class="pm-card-meta">
          <span class="mono pm-key-hint">{{ p.key_hint || '未配置 Key' }}</span>
          <span class="pm-count">{{ providerModels(p.id).length }} 个模型</span>
        </div>
        <div v-if="canManage" class="pm-card-ops">
          <el-button link type="primary" :loading="testing[p.id]" @click="onTestProvider(p)">
            <el-icon class="btn-ic"><Search /></el-icon>测试连接
          </el-button>
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
        <div class="pm-title">{{ kindLabel }}模型</div>
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
      <el-table-column label="模型名" min-width="150">
        <template #default="{ row }">
          <span class="mono">{{ row.model_name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="供应商" min-width="120">
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
    <div v-else-if="!loading && !loadError" class="pm-empty">
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
        <el-form-item label="供应商" required>
          <el-select
            v-model="providerDialog.providerType"
            :disabled="!!providerDialog.editingId"
            placeholder="选择供应商类型"
            class="pm-full"
            @change="onProviderTypeChange"
          >
            <el-option v-for="t in types" :key="t.key" :label="t.display_name" :value="t.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="显示名" required>
          <el-input v-model="providerDialog.name" maxlength="64" placeholder="空间内唯一,如 公司 DeepSeek" />
        </el-form-item>
        <el-form-item label="Base URL" required>
          <el-input v-model="providerDialog.baseUrl" placeholder="选择供应商后自动预填,可修改(https)" />
        </el-form-item>
        <el-form-item label="API Key" :required="providerKeyRequired">
          <el-input v-model="providerDialog.apiKey" type="password" show-password :placeholder="providerKeyRequired ? '必填;明文只在提交这一跳,落库即加密' : '留空 = 不修改已存 Key'" />
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
        </el-form-item>
        <el-form-item label="模型名" required>
          <el-input v-model="modelDialog.modelName" placeholder="实际调上游的名字,如 deepseek-chat" />
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
          <div class="pm-hint">OPT-29 上下文进度条的分母;常用值 4096 / 32768 / 65536 / 128000</div>
        </el-form-item>
        <el-form-item>
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
.pm-card-type {
  flex-shrink: 0;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 6px;
  background: #eef1fe;
  color: #4f6ef2;
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
</style>
