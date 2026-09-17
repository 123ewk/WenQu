<script setup lang="ts">
import type { FormInstance, FormRules } from 'element-plus'

import { errMessage, fieldErrors } from '@/api/http'
import { apiCreateKb, apiUpdateKb } from '@/api/knowledge'
import type { KnowledgeBaseOut } from '@/api/types'

/**
 * 知识库新建 / 重命名(原型 03 的两个模态框)。
 * 注意:接口不接受嵌入模型(由服务端配置),也不支持修改嵌入模型,
 * 因此不渲染模型选择器与"重建索引"流程 —— 见 对接缺口清单。
 */
const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ mode: 'create' | 'rename'; spaceId: string; kb?: KnowledgeBaseOut | null }>()
const emit = defineEmits<{ saved: [KnowledgeBaseOut] }>()

const formRef = ref<FormInstance>()
const saving = ref(false)
const form = reactive({ name: '', description: '' })
const serverErrors = reactive<Record<string, string>>({})

const isCreate = computed(() => props.mode === 'create')

const rules: FormRules = {
  name: [
    { required: true, message: '请输入知识库名称', trigger: 'blur' },
    { max: 64, message: '名称不能超过 64 个字符', trigger: 'blur' },
  ],
}

watch(visible, (v) => {
  if (!v) return
  form.name = props.kb?.name ?? ''
  form.description = props.kb?.description ?? ''
  serverErrors.name = ''
  formRef.value?.clearValidate()
})

async function onSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    const name = form.name.trim()
    const description = form.description.trim()
    const saved = isCreate.value
      ? await apiCreateKb(props.spaceId, { name, description })
      : await apiUpdateKb(props.spaceId, props.kb!.id, { name, description })
    ElMessage.success(isCreate.value ? `知识库「${name}」已创建` : '已重命名')
    visible.value = false
    emit('saved', saved)
  } catch (e) {
    const fields = fieldErrors((e as { details?: unknown }).details)
    serverErrors.name = fields.name ?? ''
    if (!fields.name) ElMessage.error(errMessage(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="isCreate ? '新建知识库' : '重命名知识库'"
    :width="isCreate ? '440px' : '400px'"
    append-to-body
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
      <el-form-item label="名称" prop="name" :error="serverErrors.name || undefined">
        <el-input v-model="form.name" maxlength="64" placeholder="例如:产品手册库" />
      </el-form-item>
      <el-form-item v-if="isCreate" label="描述">
        <el-input
          v-model="form.description"
          type="textarea"
          :rows="3"
          maxlength="200"
          placeholder="简要说明该知识库的用途(选填)"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="onSubmit">
        {{ isCreate ? '创建' : '保存' }}
      </el-button>
    </template>
  </el-dialog>
</template>
