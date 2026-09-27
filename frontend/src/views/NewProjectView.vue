<script setup lang="ts">
import { Document, Files, FolderOpened, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onBeforeMount, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'

import { api } from '@/api/client'
import type { ImportSourceType, LLMProfile, OCRProviderId, SourceLanguage } from '@/types'

type FixedOCRProviderId = Exclude<OCRProviderId, 'auto'>

const router = useRouter()
const formRef = ref<FormInstance>()
const singleInput = ref<HTMLInputElement>()
const multipleInput = ref<HTMLInputElement>()
const folderInput = ref<HTMLInputElement>()
const zipInput = ref<HTMLInputElement>()
const form = reactive({
  projectName: '',
  sourceLanguage: 'ja' as SourceLanguage,
  ocrProvider: 'mangaocr' as FixedOCRProviderId,
  llmProfileId: null as string | null,
})
const llmProfiles = ref<LLMProfile[]>([])
const sourceType = ref<ImportSourceType>('multiple')
const selectedFiles = ref<File[]>([])
const relativePaths = ref<string[]>([])
const uploading = ref(false)
const committing = ref(false)
const activeSessionId = ref<string | null>(null)
const uploadedCount = ref(0)
const progressByIndex = reactive<Record<number, number>>({})
let committed = false

const rules: FormRules = {
  projectName: [
    { required: true, message: '请输入项目名称', trigger: 'blur' },
    { min: 1, max: 120, message: '项目名称最多 120 个字符', trigger: 'blur' },
  ],
  sourceLanguage: [{ required: true, message: '请选择源语言', trigger: 'change' }],
}
const modes: Array<{
  value: ImportSourceType
  label: string
  note: string
  icon: typeof Document
}> = [
  { value: 'single', label: '单张图片', note: '导入一页漫画', icon: Document },
  { value: 'multiple', label: '多张图片', note: '一次选择多页', icon: Files },
  { value: 'folder', label: '文件夹', note: '保留目录页序', icon: FolderOpened },
  { value: 'zip', label: 'ZIP 漫画包', note: '安全解压并排序', icon: UploadFilled },
]
const supportedExtensions = new Set(['png', 'jpg', 'jpeg', 'webp'])
const totalBytes = computed(() => selectedFiles.value.reduce((sum, file) => sum + file.size, 0))
const uploadedBytes = computed(() =>
  selectedFiles.value.reduce(
    (sum, file, index) => sum + file.size * (progressByIndex[index] ?? 0),
    0,
  ),
)
const uploadPercentage = computed(() =>
  totalBytes.value ? Math.min(100, Math.round((uploadedBytes.value / totalBytes.value) * 100)) : 0,
)
const busy = computed(() => uploading.value || committing.value)
const fixedOCRProvider = computed<FixedOCRProviderId>(() =>
  form.sourceLanguage === 'ja' ? 'mangaocr' : 'paddleocr',
)
const fixedOCRProviderLabel = computed(() =>
  fixedOCRProvider.value === 'mangaocr' ? 'MangaOCR（日文）' : 'PaddleOCR（韩文 / 英文）',
)
const llmOptions = computed(() => llmProfiles.value)
watch(
  () => form.sourceLanguage,
  () => {
    form.ocrProvider = fixedOCRProvider.value
  },
  { immediate: true },
)

function formatBytes(bytes: number): string {
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

function chooseFiles(): void {
  const targets = {
    single: singleInput.value,
    multiple: multipleInput.value,
    folder: folderInput.value,
    zip: zipInput.value,
  }
  targets[sourceType.value]?.click()
}

function resetSelection(): void {
  selectedFiles.value = []
  relativePaths.value = []
  for (const key of Object.keys(progressByIndex)) delete progressByIndex[Number(key)]
}

function onModeChange(value: ImportSourceType): void {
  if (busy.value) return
  sourceType.value = value
  resetSelection()
}

function onFilesSelected(event: Event): void {
  const input = event.target as HTMLInputElement
  const incoming = Array.from(input.files ?? [])
  input.value = ''
  if (!incoming.length) return
  if (sourceType.value === 'zip') {
    if (incoming.length !== 1 || !incoming[0].name.toLowerCase().endsWith('.zip')) {
      ElMessage.error('请选择一个 ZIP 文件')
      return
    }
  } else {
    const invalid = incoming.find(
      (file) => !supportedExtensions.has(file.name.split('.').pop()?.toLowerCase() ?? ''),
    )
    if (invalid) {
      ElMessage.error(`不支持文件：${invalid.name}`)
      return
    }
  }
  selectedFiles.value = sourceType.value === 'single' ? incoming.slice(0, 1) : incoming
  relativePaths.value = selectedFiles.value.map((file) =>
    sourceType.value === 'folder' && file.webkitRelativePath ? file.webkitRelativePath : file.name,
  )
}

async function uploadWithConcurrency(sessionId: string): Promise<void> {
  let cursor = 0
  const worker = async () => {
    while (cursor < selectedFiles.value.length) {
      const index = cursor++
      const file = selectedFiles.value[index]
      await api.uploadImportFile(sessionId, file, relativePaths.value[index], (event) => {
        progressByIndex[index] = event.total ? Math.min(1, event.loaded / event.total) : 0
      })
      progressByIndex[index] = 1
      uploadedCount.value += 1
    }
  }
  await Promise.all(Array.from({ length: Math.min(2, selectedFiles.value.length) }, worker))
}

async function createProject(): Promise<void> {
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (!selectedFiles.value.length) {
    ElMessage.warning('请先选择漫画文件')
    return
  }
  uploading.value = true
  uploadedCount.value = 0
  try {
    const session = await api.createImportSession({
      project_name: form.projectName,
      source_language: form.sourceLanguage,
      target_language: 'zh-CN',
      source_type: sourceType.value,
      ocr_provider: form.ocrProvider,
      llm_profile_id: form.llmProfileId,
    })
    activeSessionId.value = session.id
    await uploadWithConcurrency(session.id)
    uploading.value = false
    committing.value = true
    const result = await api.commitImportSession(session.id)
    committed = true
    activeSessionId.value = null
    ElMessage.success('项目已创建，正在后台生成预览')
    await router.replace({
      name: 'project',
      params: { id: result.project_id },
      query: { task: result.task_id },
    })
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '项目创建失败')
  } finally {
    uploading.value = false
    committing.value = false
  }
}

async function cancelUpload(): Promise<void> {
  if (!activeSessionId.value) return
  try {
    await api.deleteImportSession(activeSessionId.value)
    activeSessionId.value = null
    resetSelection()
    ElMessage.success('本次上传已取消')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法取消上传')
  }
}

onBeforeMount(async () => {
  try {
    const [profiles, settings] = await Promise.all([
      api.listLLMProfiles(),
      api.getTranslationSettings(),
    ])
    llmProfiles.value = profiles.items
    form.llmProfileId = settings.default_profile_id
  } catch {
    // API Profiles are optional; the project can still be created.
  }
})

onBeforeRouteLeave(async () => {
  if (!busy.value || committed) return true
  try {
    await ElMessageBox.confirm('上传尚未完成，离开会丢弃本次导入。', '离开此页面？', {
      confirmButtonText: '离开并丢弃',
      cancelButtonText: '继续上传',
      type: 'warning',
    })
    if (activeSessionId.value)
      await api.deleteImportSession(activeSessionId.value).catch(() => undefined)
    return true
  } catch {
    return false
  }
})

onBeforeUnmount(() => {
  // The route guard owns session cleanup; there are no retained File/Blob URLs here.
})
</script>

<template>
  <div class="page-shell new-project-page">
    <section class="page-heading compact-heading">
      <div>
        <h1>开始一本漫画</h1>
      </div>
    </section>

    <div class="creation-layout">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" class="project-form">
        <section class="form-section">
          <span class="section-number">01</span>
          <div class="section-content">
            <h2>项目信息</h2>
            <div class="field-grid">
              <el-form-item label="项目名称" prop="projectName">
                <el-input
                  v-model="form.projectName"
                  maxlength="120"
                  placeholder="例如：夏日短篇 第 1 话"
                  size="large"
                />
              </el-form-item>
              <el-form-item label="源语言" prop="sourceLanguage">
                <el-select v-model="form.sourceLanguage" size="large">
                  <el-option label="日文" value="ja" />
                  <el-option label="韩文" value="ko" />
                  <el-option label="英文" value="en" />
                </el-select>
              </el-form-item>
              <el-form-item label="翻译 Profile（可选）">
                <el-select
                  v-model="form.llmProfileId"
                  clearable
                  size="large"
                  placeholder="稍后再配置"
                >
                  <el-option
                    v-for="profile in llmOptions"
                    :key="profile.id"
                    :label="`${profile.name} · ${profile.provider}`"
                    :value="profile.id"
                  />
                </el-select>
              </el-form-item>
            </div>
          </div>
        </section>

        <section class="form-section">
          <span class="section-number">02</span>
          <div class="section-content">
            <h2>漫画来源</h2>
            <div class="source-modes">
              <button
                v-for="mode in modes"
                :key="mode.value"
                class="source-mode"
                :class="{ selected: sourceType === mode.value }"
                type="button"
                :disabled="busy"
                @click="onModeChange(mode.value)"
              >
                <el-icon><component :is="mode.icon" /></el-icon>
                <strong>{{ mode.label }}</strong>
                <span>{{ mode.note }}</span>
              </button>
            </div>
            <input
              ref="singleInput"
              class="visually-hidden"
              type="file"
              accept=".png,.jpg,.jpeg,.webp"
              aria-hidden="true"
              tabindex="-1"
              @change="onFilesSelected"
            />
            <input
              ref="multipleInput"
              class="visually-hidden"
              type="file"
              accept=".png,.jpg,.jpeg,.webp"
              multiple
              aria-hidden="true"
              tabindex="-1"
              @change="onFilesSelected"
            />
            <input
              ref="folderInput"
              class="visually-hidden"
              type="file"
              accept=".png,.jpg,.jpeg,.webp"
              multiple
              webkitdirectory
              aria-hidden="true"
              tabindex="-1"
              @change="onFilesSelected"
            />
            <input
              ref="zipInput"
              class="visually-hidden"
              type="file"
              accept=".zip,application/zip"
              aria-hidden="true"
              tabindex="-1"
              @change="onFilesSelected"
            />

            <button class="file-picker" type="button" :disabled="busy" @click="chooseFiles">
              <el-icon><FolderOpened /></el-icon>
              <span>{{ selectedFiles.length ? '重新选择' : '打开本地文件' }}</span>
            </button>
            <p class="source-safety-note">源文件只会复制到工作区，原始内容不会被修改。</p>

            <div v-if="selectedFiles.length" class="selection-summary">
              <div>
                <strong>{{ selectedFiles.length }} 个文件</strong>
                <span>{{ formatBytes(totalBytes) }}</span>
              </div>
              <ol>
                <li v-for="(path, index) in relativePaths.slice(0, 5)" :key="`${path}-${index}`">
                  <span>{{ path }}</span>
                  <small>{{ formatBytes(selectedFiles[index].size) }}</small>
                </li>
              </ol>
              <div v-if="selectedFiles.length > 5" class="more-files">
                另有 {{ selectedFiles.length - 5 }} 个文件
              </div>
            </div>
          </div>
        </section>
      </el-form>

      <aside class="creation-summary">
        <h2>创建确认</h2>
        <dl>
          <div>
            <dt>项目</dt>
            <dd>{{ form.projectName || '尚未命名' }}</dd>
          </div>
          <div>
            <dt>语言</dt>
            <dd>{{ { ja: '日文', ko: '韩文', en: '英文' }[form.sourceLanguage] }} → 简中</dd>
          </div>
          <div>
            <dt>页面文件</dt>
            <dd>{{ selectedFiles.length || '—' }}</dd>
          </div>
          <div>
            <dt>OCR</dt>
            <dd>{{ fixedOCRProviderLabel }}</dd>
          </div>
          <div>
            <dt>翻译</dt>
            <dd>
              {{ llmProfiles.find((item) => item.id === form.llmProfileId)?.name || '稍后配置' }}
            </dd>
          </div>
          <div>
            <dt>文件大小</dt>
            <dd>{{ selectedFiles.length ? formatBytes(totalBytes) : '—' }}</dd>
          </div>
        </dl>
        <div v-if="busy" class="upload-progress">
          <div>
            <span>{{
              committing ? '正在建立项目' : `正在上传 ${uploadedCount}/${selectedFiles.length}`
            }}</span
            ><strong>{{ uploadPercentage }}%</strong>
          </div>
          <el-progress :percentage="uploadPercentage" :show-text="false" :stroke-width="8" />
        </div>
        <el-button
          type="primary"
          size="large"
          :loading="busy"
          :disabled="!selectedFiles.length"
          @click="createProject"
        >
          {{ committing ? '正在提交…' : uploading ? '正在上传…' : '创建并导入' }}
        </el-button>
        <el-button v-if="activeSessionId && uploading" plain @click="cancelUpload"
          >取消上传</el-button
        >
      </aside>
    </div>
  </div>
</template>
