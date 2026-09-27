<script setup lang="ts">
/**
 * OPT-27 模型配置页(对接说明 §1),区块①~④:
 * ① Embedding(内置模型锁死,只配 Key) ②③ chat 供应商+模型 ④ Rerank 开关+供应商/模型。
 * 非 Admin 进页面 = 只读(写按钮隐藏);路由用扁平 /settings/models,空间上下文走侧栏切换器
 * (与既有页面一致,不采用文档建议的 /space/:sid/... 形态)。
 */
import { Link, WarningFilled } from '@element-plus/icons-vue'

import { apiGetLangfuseConfig, apiPutLangfuseConfig } from '@/api/langfuseConfig'
import { apiGetModelConfig, apiGetProviderTypes, apiPutEmbeddingKey, apiPutRerankSwitch, apiTestEmbeddingKey, modelConfigErrorHint } from '@/api/modelConfig'
import { errMessage } from '@/api/http'
import type { LangfuseConfigOut, ProviderTypeOut, SpaceModelConfigOut } from '@/api/types'
import ProvidersModelsBlock from './ProvidersModelsBlock.vue'
import { useAuthStore } from '@/stores/auth'
import { isAtLeast, ROLE } from '@/utils/roles'

const auth = useAuthStore()

/** 写操作门槛:成员身份 ≥30(Admin+),与 OPT-26 建 Key 同级(§1 角色口径) */
const canManage = computed(() => isAtLeast(auth.role, ROLE.ADMIN))

const config = ref<SpaceModelConfigOut | null>(null)
const types = ref<ProviderTypeOut[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

/** 唯一的读口:聚合 GET /model-config(契约里没有 providers/models 列表接口);
 *  任何写操作完成后都重拉这里(掩码/默认态只由后端给) */
async function load() {
  const spaceId = auth.currentSpaceId
  if (!spaceId) {
    config.value = null
    return
  }
  loading.value = true
  loadError.value = null
  try {
    const [cfg, t] = await Promise.all([apiGetModelConfig(spaceId), apiGetProviderTypes(spaceId)])
    config.value = cfg
    types.value = t
  } catch (e) {
    loadError.value = errMessage(e)
    config.value = null
  } finally {
    loading.value = false
  }
}

watch(() => auth.currentSpaceId, load, { immediate: true })

/* ───── OPT-49:Langfuse 可观测配置(全局 superuser 专属;探测 403 则整卡不渲染) ───── */

const langfuse = ref<LangfuseConfigOut | null>(null)
const lfHost = ref('')
const lfPublicKey = ref('')
const lfSecretInput = ref('')
const lfClearSecret = ref(false)
const lfSaving = ref(false)

async function loadLangfuse() {
  try {
    langfuse.value = await apiGetLangfuseConfig()
    lfHost.value = langfuse.value.host
    lfPublicKey.value = langfuse.value.public_key
    lfSecretInput.value = ''
    lfClearSecret.value = false
  } catch {
    langfuse.value = null // 403(非 superuser)等 → 整卡隐藏,不影响本页空间配置
  }
}

async function onSaveLangfuse() {
  if (!langfuse.value || lfSaving.value) return
  // 密钥三态:勾了清除 → 空串;输入了新值 → 替换;否则 → null(不改动)
  const secret = lfClearSecret.value ? '' : lfSecretInput.value || null
  try {
    lfSaving.value = true
    langfuse.value = await apiPutLangfuseConfig({
      host: lfHost.value,
      public_key: lfPublicKey.value,
      secret_key: secret,
      enabled: langfuse.value.enabled,
    })
    lfSecretInput.value = ''
    lfClearSecret.value = false
    ElMessage.success('Langfuse 配置已保存')
  } catch (e) {
    ElMessage.error(errMessage(e))
  } finally {
    lfSaving.value = false
  }
}

async function onToggleLangfuse(): Promise<boolean> {
  const current = langfuse.value
  if (!current || lfSaving.value || !auth.currentSpaceId) return false
  const opening = !current.enabled
  if (opening && !current.key_hint) {
    ElMessage.warning('先保存 Secret Key,再开启上送')
    return false
  }
  try {
    await ElMessageBox.confirm(
      opening
        ? '开启后,本实例所有问答的 trace(含问题与答案原文)将上送到 Langfuse。确认开启?'
        : '关闭后停止上送 trace(已存数据保留在 Langfuse)。确认关闭?',
      opening ? '开启 Langfuse 上送' : '关闭 Langfuse 上送',
      { confirmButtonText: opening ? '确认开启' : '确认关闭', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return false
  }
  try {
    lfSaving.value = true
    langfuse.value = await apiPutLangfuseConfig({
      host: lfHost.value,
      public_key: lfPublicKey.value,
      secret_key: null, // 开关切换不动密钥
      enabled: opening,
    })
    return true
  } catch (e) {
    ElMessage.error(errMessage(e))
    return false
  } finally {
    lfSaving.value = false
  }
}

onMounted(loadLangfuse)

/* ───── 区块①:Embedding Key ───── */

const embKeyInput = ref('')
const embTesting = ref(false)
const embSaving = ref(false)

const embedding = computed(() => config.value?.embedding ?? null)

/** 未配 Key 的展示分流:fallback_active=true(dev 回落全局 Key)= 灰色提示;否则生产 = 红色警示。
 *  注意它在 embedding 卡片对象里,不在聚合顶层;后端不告诉前端环境,判据只有这两个字段 */
const embMissingProd = computed(() => !!embedding.value && !embedding.value.key_configured && !embedding.value.fallback_active)
const embFallbackDev = computed(() => !!embedding.value && !embedding.value.key_configured && embedding.value.fallback_active)

async function onTestEmbedding() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || embTesting.value) return
  embTesting.value = true
  try {
    // 输入框为空时不传 api_key = 测已存 Key(§1.2);一把 Key 都没有时后端给 503,message 照展示
    await apiTestEmbeddingKey(spaceId, embKeyInput.value ? { api_key: embKeyInput.value } : {})
    ElMessage.success('向量化 Key 可用')
  } catch (e) {
    ElMessage.error(`测试失败:${modelConfigErrorHint(e)}`)
  } finally {
    embTesting.value = false
  }
}

async function onSaveEmbedding() {
  const spaceId = auth.currentSpaceId
  if (!spaceId || embSaving.value) return
  const key = embKeyInput.value.trim()
  if (!key) {
    // 空串(纯空白后端等价空串)= 清除本空间 Key,必须二次确认(§1.2);后果按 dev/prod 口径讲清
    const confirmed = await ElMessageBox.confirm(
      '保存空值将清除本空间的向量化 Key。清除后:生产环境上传入库直接不可用;开发环境会回落到运维全局 Key。确认清除?',
      '清除向量化 Key',
      { type: 'warning', confirmButtonText: '确认清除', cancelButtonText: '取消' },
    ).catch(() => false)
    if (!confirmed) return
  }
  embSaving.value = true
  try {
    // 只回 {ok:true};key_configured/key_hint 必须重拉聚合才拿到,别在本地拼掩码
    await apiPutEmbeddingKey(spaceId, { api_key: key })
    await load()
    embKeyInput.value = ''
    ElMessage.success(key ? '向量化 Key 已保存' : '向量化 Key 已清除')
  } catch (e) {
    ElMessage.error(modelConfigErrorHint(e))
  } finally {
    embSaving.value = false
  }
}

/* ───── 区块④:Rerank 开关 ───── */

const switchSaving = ref(false)

const rerank = computed(() => config.value?.rerank ?? null)
const rerankEnabled = computed(() => rerank.value?.enabled ?? false)
/** 开关打开但没有默认重排模型 = 后端等同关闭(§1.5),给黄色提示;default_model 来自聚合 */
const rerankNoDefault = computed(() => rerankEnabled.value && !rerank.value?.default_model)

async function onRerankSwitch(v: boolean) {
  const spaceId = auth.currentSpaceId
  if (!spaceId || !config.value || switchSaving.value) return
  switchSaving.value = true
  try {
    // 只回 {ok:true};开关与"是否已配模型"解耦,拨完重拉聚合对齐 default_model 提示
    await apiPutRerankSwitch(spaceId, { enabled: v })
    await load()
  } catch (e) {
    await load()
    ElMessage.error(modelConfigErrorHint(e))
  } finally {
    switchSaving.value = false
  }
}
</script>

<template>
  <div class="page-pad">
    <div class="mc-wrap" v-loading="loading">
      <div class="page-head-row">
        <div>
          <div class="page-title">模型配置</div>
          <div class="page-sub">本空间自配模型与 Key;未配置的模型调用回落全局默认</div>
        </div>
      </div>

      <div v-if="!canManage" class="mc-note">
        当前角色为只读。模型配置需空间 <b>Admin 及以上</b> 成员管理,如需变更请联系空间管理员。
      </div>

      <div v-if="loadError" class="mc-note mc-note-err">
        加载失败:{{ loadError }}
        <el-button size="small" @click="load">重试</el-button>
      </div>

      <!-- 区块①:Embedding -->
      <div v-if="embedding" class="card mc-emb" data-tour="tour-models">
        <div class="mc-title">向量化(Embedding)</div>
        <div class="mc-sub">模型与维度由入库向量兼容性决定,已锁死不可更换;只需配置本空间的 API Key</div>

        <div class="mc-kv">
          <div class="mc-kv-row"><span class="mc-k">模型</span><span class="mc-v mono">{{ embedding.model_name }}</span></div>
          <div class="mc-kv-row"><span class="mc-k">维度</span><span class="mc-v">{{ embedding.dims }}<span class="mc-kv-note">(换模型会导致已入库向量失效,故锁死)</span></span></div>
          <div class="mc-kv-row">
            <span class="mc-k">官网</span>
            <a class="mc-v mc-link" :href="embedding.doc_url" target="_blank" rel="noopener">
              <el-icon class="btn-ic"><Link /></el-icon>前往控制台创建 API Key
            </a>
          </div>
          <div class="mc-kv-row">
            <span class="mc-k">状态</span>
            <span v-if="embedding.key_configured" class="mc-v">
              已配置 <span class="mono mc-hint">{{ embedding.key_hint }}</span>
            </span>
            <span v-else-if="embFallbackDev" class="mc-v mc-dev-fallback">未配置 · 开发环境回落全局运维 Key(仅 dev)</span>
            <span v-else class="mc-v mc-bad">未配置</span>
          </div>
        </div>

        <div v-if="embMissingProd" class="mc-warn mc-warn-red">
          <el-icon class="btn-ic"><WarningFilled /></el-icon>
          空间未配置向量化 Key,上传入库与检索不可用,请联系空间管理员
        </div>

        <div v-if="canManage" class="mc-emb-ops">
          <el-input
            v-model="embKeyInput"
            type="password"
            show-password
            class="mc-emb-input"
            :placeholder="embedding.key_configured ? '填入新 Key;留空保存 = 清除本空间 Key' : '填入在官网创建的 API Key'"
          />
          <el-button :loading="embTesting" @click="onTestEmbedding">测试</el-button>
          <el-button type="primary" :loading="embSaving" @click="onSaveEmbedding">保存</el-button>
        </div>
      </div>

      <!-- 区块②③:chat 供应商 + 模型(数据都来自上方聚合视图,写成功后重拉) -->
      <ProvidersModelsBlock
        v-if="config"
        kind="chat"
        :can-manage="canManage"
        :config="config"
        :types="types"
        @changed="load"
      />

      <!-- 区块④:Rerank 开关 + 其供应商/模型(同款形状,kind 过滤) -->
      <div v-if="rerank" class="card mc-rerank">
        <div class="mc-rerank-head">
          <div>
            <div class="mc-title">重排(Rerank)</div>
            <div class="mc-sub">开启后,检索结果会经由重排模型重新排序;关闭时不参与链路。上游故障时自动降级为不重排,不影响问答</div>
          </div>
          <el-switch
            :model-value="rerankEnabled"
            :disabled="!canManage || switchSaving"
            :loading="switchSaving"
            @update:model-value="(v: any) => onRerankSwitch(Boolean(v))"
          />
        </div>
        <div v-if="rerankNoDefault" class="mc-warn mc-warn-yellow">
          重排已开启,但还没有默认重排模型 —— 实际检索等同关闭,请在下方配置模型并设为默认
        </div>
      </div>
      <ProvidersModelsBlock
        v-if="config"
        kind="rerank"
        hide-kind-label
        :can-manage="canManage"
        :config="config"
        :types="types"
        @changed="load"
      />

      <!-- OPT-49:Langfuse 可观测(全局 superuser 专属;探测 403 自动隐藏) -->
      <div v-if="langfuse" class="card mc-emb" data-testid="langfuse-card">
        <div class="mc-title">Langfuse 可观测</div>
        <div class="mc-sub">全链路 trace 上送(问题/答案/检索片段,数据存自托管 Langfuse);未开启时零开销</div>

        <div class="lf-ops">
          <el-input v-model="lfHost" class="lf-input" placeholder="Langfuse 地址,如 https://langfuse.internal.example">
            <template #prepend>地址</template>
          </el-input>
          <el-input v-model="lfPublicKey" class="lf-input" placeholder="Public Key(pk-lf-...)">
            <template #prepend>公钥</template>
          </el-input>
          <el-input
            v-model="lfSecretInput"
            type="password"
            show-password
            class="lf-input"
            :placeholder="langfuse.key_hint ? `已配置 ${langfuse.key_hint} · 留空 = 不改动` : 'Secret Key(sk-lf-...)'"
          >
            <template #prepend>私钥</template>
          </el-input>
        </div>
        <div v-if="langfuse.key_hint" class="lf-clear">
          <el-checkbox v-model="lfClearSecret">清除已存密钥</el-checkbox>
        </div>

        <div class="lf-ops lf-switch-row">
          <div class="lf-switch-text">
            <span>上送开关</span>
            <span class="lf-hint">
              {{ langfuse.enabled ? '已开启' : '已关闭' }}
              <template v-if="langfuse.updated_at && langfuse.updated_by_hint">
                · 由 {{ langfuse.updated_by_hint }} 于 {{ langfuse.updated_at }} 变更
              </template>
            </span>
          </div>
          <el-switch
            :model-value="langfuse.enabled"
            :before-change="onToggleLangfuse"
            :loading="lfSaving"
            data-testid="langfuse-switch"
          />
        </div>

        <div class="lf-save">
          <el-button type="primary" :loading="lfSaving" @click="onSaveLangfuse">保存配置</el-button>
          <a class="lf-link" href="https://cloud.langfuse.com" target="_blank" rel="noopener">
            <el-icon class="btn-ic"><Link /></el-icon>没有 Key?去 Langfuse 注册申请
          </a>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.btn-ic {
  margin-right: 4px;
}
.mc-wrap {
  max-width: 980px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.mc-note {
  padding: 12px 16px;
  border-radius: 8px;
  background: #f7f8fa;
  font-size: 13px;
  line-height: 1.6;
  color: #6b7280;
}
.mc-note-err {
  color: #ef4444;
}
.mc-title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}
.mc-sub {
  margin-top: 3px;
  font-size: 12px;
  color: #9ca3af;
  line-height: 1.5;
}
.mc-emb,
.mc-rerank {
  padding: 20px;
}
.mc-kv {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.mc-kv-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 13px;
}
.mc-k {
  width: 64px;
  flex-shrink: 0;
  color: #9ca3af;
}
.mc-v {
  color: #111827;
}
.mc-kv-note {
  margin-left: 6px;
  font-size: 12px;
  color: #9ca3af;
}
.mc-link {
  display: inline-flex;
  align-items: center;
  color: #4f6ef2;
  text-decoration: none;
}
.lf-ops {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.lf-input {
  max-width: 560px;
}
.lf-clear {
  margin-top: 6px;
}
.lf-switch-row {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  max-width: 560px;
}
.lf-switch-text {
  display: flex;
  flex-direction: column;
}
.lf-hint {
  font-size: 12px;
  color: #9ca3af;
}
.lf-save {
  margin-top: 14px;
  display: flex;
  align-items: center;
  gap: 16px;
}
.lf-link {
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  color: #4f6ef2;
  text-decoration: none;
}
.mc-link:hover {
  text-decoration: underline;
}
.mc-hint {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  background: #f7f8fa;
  font-size: 12px;
  color: #374151;
}
.mc-bad {
  color: #ef4444;
}
.mc-dev-fallback {
  color: #9ca3af;
}
.mc-warn {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
  display: flex;
  align-items: flex-start;
}
.mc-warn-red {
  border: 1px solid rgba(239, 68, 68, 0.3);
  background: rgba(239, 68, 68, 0.05);
  color: #ef4444;
}
.mc-warn-yellow {
  border: 1px solid rgba(217, 119, 6, 0.3);
  background: rgba(245, 158, 11, 0.06);
  color: #d97706;
}
.mc-emb-ops {
  margin-top: 16px;
  display: flex;
  gap: 8px;
}
.mc-emb-input {
  max-width: 420px;
}
.mc-rerank-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
</style>
