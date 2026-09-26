<script setup lang="ts">
/**
 * OPT-48 实例级档位灰度开关(路由 /settings/permission-enforcement):
 * - 谁能看/拨:任意空间 Admin+(role≥30)或 superuser;入口按"任意空间 Admin+"显隐
 *   (纯 superuser 且没有任何空间身份的账号看不到入口,但 API 仍可调);
 * - **效果是全局的(D1)**:打开 = 整个实例所有空间的成员在问答/检索/文档等核心接口
 *   都必须有生效档位 —— 页面上必须让用户明白这不是"当前空间"的开关;
 * - 403 = 没资格;409 = 有资格但开启者本人在某些空间缺生效档位(先给自己配好档再开);
 * - 打开后本进程立即生效,其它实例进程最多 60s 后跟上(缓存 TTL,不造成越权)。
 */
import { RefreshRight, WarningFilled } from '@element-plus/icons-vue'

import { apiGetPermissionEnforcement, apiPutPermissionEnforcement } from '@/api/permissionEnforcement'
import { ApiError, errMessage } from '@/api/http'
import type { EnforcementSpaceGapOut, EnforcementStatusOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { fmtDateTime } from '@/utils/format'
import { isAtLeast, ROLE } from '@/utils/roles'

const auth = useAuthStore()

/** 与后端守卫同口径的"任意空间 Admin+"(UserOut 不含 is_superuser,纯 superuser 账号靠 API 直调) */
const canManage = computed(() => auth.spaces.some((s) => isAtLeast(s.role, ROLE.ADMIN)))

const status = ref<EnforcementStatusOut | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
const forbidden = ref(false)
const saving = ref(false)

const sourceLabel = computed(() => {
  if (!status.value) return ''
  return status.value.source === 'db'
    ? '页面配置(库里已保存)'
    : '环境变量兜底(页面从未拨过,改环境变量+重启才会变)'
})

const updatedLabel = computed(() => {
  const s = status.value
  if (!s || s.source !== 'db') return ''
  const who = s.updated_by_hint ? `由 ${s.updated_by_hint} ` : ''
  return `${who}于 ${fmtDateTime(s.updated_at)} 拨动`
})

async function load() {
  loading.value = true
  loadError.value = null
  forbidden.value = false
  try {
    status.value = await apiGetPermissionEnforcement()
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) forbidden.value = true
    else loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

/** 409 details.spaces → 可读的"缺档空间"清单文案 */
function gapNames(details: unknown): string {
  const spaces = (details as { spaces?: EnforcementSpaceGapOut[] } | null)?.spaces ?? []
  return spaces.map((g) => g.space_name).join('、') || '(未返回空间清单)'
}

async function confirmToggle(): Promise<boolean> {
  const current = status.value
  if (!current || saving.value) return false
  const opening = !current.effective
  const impact = current.impact
  const message = opening
    ? `打开后,整个实例所有空间的成员在问答/检索/文档等核心接口都必须有"生效档位",` +
      `没有的人会立即被拦(当前全实例 ${impact.members_total} 条成员关系中有 ` +
      `${impact.members_without_active_group} 条没有生效档)。确认打开?`
    : '关闭后,人链路恢复只看空间角色(程序化 API Key 不受影响,一直按档位收口)。确认关闭?'
  try {
    await ElMessageBox.confirm(message, opening ? '打开档位强制 · 影响整个实例' : '关闭档位强制', {
      confirmButtonText: opening ? '确认打开' : '确认关闭',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return false
  }
  try {
    saving.value = true
    status.value = await apiPutPermissionEnforcement({ enabled: opening })
    ElMessage.success(opening ? '已打开:本进程已生效,其它实例进程最多 60 秒后跟上' : '已关闭:人链路恢复只看角色')
    return true
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      ElMessage.error(`打开前先给自己配好生效档位:你在这些空间还没有生效档位 —— ${gapNames(e.details)}`)
    } else {
      ElMessage.error(errMessage(e))
    }
    return false
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-pad">
    <div class="pe-wrap" v-loading="loading">
      <div class="page-head-row">
        <div>
          <div class="page-title">档位灰度开关</div>
          <div class="page-sub">控制"人链路是否必须有权位档位" · 影响整个实例所有空间,不只是当前空间</div>
        </div>
        <div class="pe-tools">
          <el-button size="small" @click="load">
            <el-icon class="btn-ic"><RefreshRight /></el-icon>刷新
          </el-button>
        </div>
      </div>

      <el-alert
        class="pe-alert"
        type="warning"
        :closable="false"
        show-icon
        title="这是实例级开关:打开会影响整个实例所有空间的全部真人成员"
        description="程序化调用(X-API-Key)不受此开关影响,一直都按档位收口。打开前请先给成员配好档位并设为当前档。"
      />

      <div v-if="loadError" class="pe-error">
        <el-icon class="btn-ic"><WarningFilled /></el-icon>{{ loadError }}
      </div>

      <div v-else-if="forbidden || !canManage" class="card pe-empty">
        仅空间管理员(Admin)及以上可以查看和拨动这个实例级开关。
      </div>

      <template v-else-if="status">
        <div class="card pe-state">
          <div class="pe-state-main">
            <div>
              <div class="pe-state-title">{{ status.effective ? '已打开:核心接口要求生效档位' : '已关闭:人只看空间角色' }}</div>
              <div class="pe-state-cap">
                当前值来源:{{ sourceLabel }}<template v-if="updatedLabel"> · {{ updatedLabel }}</template>
              </div>
            </div>
            <el-switch
              :model-value="status.effective"
              :before-change="confirmToggle"
              :loading="saving"
              size="large"
              data-testid="enforcement-switch"
            />
          </div>
        </div>

        <div class="card pe-impact">
          <div class="pe-impact-title">影响面(全实例)</div>
          <div class="pe-impact-grid">
            <div class="pe-impact-item">
              <div class="pe-num">{{ status.impact.spaces }}</div>
              <div class="pe-cap">空间</div>
            </div>
            <div class="pe-impact-item">
              <div class="pe-num">{{ status.impact.members_total }}</div>
              <div class="pe-cap">成员关系</div>
            </div>
            <div class="pe-impact-item">
              <div class="pe-num pe-num-warn">{{ status.impact.members_without_active_group }}</div>
              <div class="pe-cap">没有生效档(打开后会立即被拦)</div>
            </div>
          </div>
          <div v-if="status.impact.actor_spaces_without_group.length" class="pe-self-gap">
            你在这些空间还没有生效档位,打开前请先给自己配好:
            {{ status.impact.actor_spaces_without_group.map((g) => g.space_name).join('、') }}
          </div>
          <div v-else class="pe-self-ok">你所在的空间都已配置生效档位,可以安全打开。</div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.pe-wrap {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 880px;
}
.pe-tools {
  display: flex;
  gap: 8px;
}
.pe-alert {
  border-radius: 8px;
}
.pe-error {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--el-color-danger);
}
.pe-empty {
  color: var(--el-text-color-secondary);
}
.pe-state-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.pe-state-title {
  font-weight: 600;
  font-size: 15px;
}
.pe-state-cap {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pe-impact-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 12px;
}
.pe-impact-grid {
  display: flex;
  gap: 32px;
}
.pe-num {
  font-size: 24px;
  font-weight: 600;
}
.pe-num-warn {
  color: var(--el-color-warning);
}
.pe-cap {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.pe-self-gap {
  margin-top: 12px;
  font-size: 13px;
  color: var(--el-color-warning);
}
.pe-self-ok {
  margin-top: 12px;
  font-size: 13px;
  color: var(--el-color-success);
}
</style>
