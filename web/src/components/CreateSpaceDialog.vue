<script setup lang="ts">
import type { FormInstance, FormRules } from 'element-plus'

import { errMessage, fieldErrors } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const visible = defineModel<boolean>({ default: false })

const auth = useAuthStore()
const formRef = ref<FormInstance>()
const saving = ref(false)
const form = reactive({ name: '', description: '' })
const serverErrors = reactive<Record<string, string>>({})

const rules: FormRules = {
  name: [
    { required: true, message: '请输入空间名称', trigger: 'blur' },
    { max: 64, message: '空间名称不能超过 64 个字符', trigger: 'blur' },
  ],
}

watch(visible, (v) => {
  if (v) {
    form.name = ''
    form.description = ''
    serverErrors.name = ''
    formRef.value?.clearValidate()
  }
})

async function onSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    const name = form.name.trim()
    await auth.createSpace(name, form.description.trim())
    ElMessage.success(`空间「${name}」已创建`)
    visible.value = false
  } catch (e) {
    const fields = fieldErrors((e as { details?: unknown }).details)
    if (fields.name) {
      serverErrors.name = fields.name
    } else {
      ElMessage.error(errMessage(e))
    }
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="新建空间" width="420px" append-to-body>
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
      <el-form-item label="空间名称" prop="name" :error="serverErrors.name || undefined">
        <el-input v-model="form.name" placeholder="例如:产品研发部" maxlength="64" @input="serverErrors.name = ''" />
      </el-form-item>
      <el-form-item label="描述(可选)">
        <el-input
          v-model="form.description"
          type="textarea"
          :rows="3"
          placeholder="这个空间用来做什么"
          maxlength="200"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="onSubmit">创建</el-button>
    </template>
  </el-dialog>
</template>
