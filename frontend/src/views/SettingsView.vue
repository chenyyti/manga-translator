<script setup lang="ts">
import { Cpu, DataLine, FolderOpened, Lock, Plus, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import { useDetectionPreparation } from '@/composables/useDetectionPreparation'
import type {
  DetectionModel,
  DetectionRuntime,
  DetectionSettings,
  OCRProviderInfo,
  OCRSettings,
  LLMProfile,
  LLMProviderInfo,
  TranslationSettings,
  RuntimeSettings,
  FontInfo,
  InpaintingProviderInfo,
  RenderSettings,
  PerformanceSettings,
} from '@/types'

const runtime = ref<RuntimeSettings | null>(null)
const detectionRuntime = ref<DetectionRuntime | null>(null)
const detectionRuntimePending = ref(true)
const models = ref<DetectionModel[]>([])
const detection = ref<DetectionSettings | null>(null)
const ocrProviders = ref<OCRProviderInfo[]>([])
const ocr = ref<OCRSettings | null>(null)
const llmProviders = ref<LLMProviderInfo[]>([])
const profiles = ref<LLMProfile[]>([])
const translation = ref<TranslationSettings | null>(null)
const inpaintingProviders = ref<InpaintingProviderInfo[]>([])
const renderSettings = ref<RenderSettings | null>(null)
const fonts = ref<FontInfo[]>([])
const performance = ref<PerformanceSettings | null>(null)
const fontInput = ref<HTMLInputElement | null>(null)
const savingRender = ref(false)
const savingPerformance = ref(false)
const uploadingFont = ref(false)
const profileForm = ref({
  name: '',
  provider: 'openai' as LLMProfile['provider'],
  base_url: '',
  model: '',
  api_key: '',
  temperature: null as number | null,
  max_tokens: 2048,
  timeout_seconds: 60,
  max_concurrency: 3,
})
const savingProfile = ref(false)
const editingProfileId = ref<string | null>(null)
type SettingsSection = 'overview' | 'detection' | 'ocr' | 'translation' | 'render' | 'queue'
const pending = ref<Record<SettingsSection, boolean>>({
  overview: true,
  detection: true,
  ocr: true,
  translation: true,
  render: true,
  queue: true,
})
const loadErrors = ref<Partial<Record<SettingsSection, string>>>({})
const saving = ref(false)
const uploading = ref(false)
const savingOCR = ref(false)
const uploadProgress = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)
const modelName = ref('')
const runtimeReady = computed(
  () => detectionRuntime.value?.torch_installed && detectionRuntime.value?.ultralytics_installed,
)

const detectionModelStatusLabels: Record<string, string> = {
  ready: '已就绪',
  preparing: '准备中',
  invalid: '无效',
  unavailable: '不可用',
  missing: '文件缺失',
}

function detectionModelStatusLabel(model: DetectionModel): string {
  return detectionModelStatusLabels[model.status] ?? model.status
}

function detectionModelOptionLabel(model: DetectionModel): string {
  return `${model.name} · ${model.filename} · ${detectionModelStatusLabel(model)}`
}

async function loadSection<T>(
  section: SettingsSection,
  request: Promise<T>,
  apply: (result: T) => void,
): Promise<void> {
  try {
    apply(await request)
  } catch (error) {
    loadErrors.value[section] = error instanceof Error ? error.message : '设置加载失败'
  } finally {
    pending.value[section] = false
  }
}

function load(): void {
  void loadSection('overview', api.getRuntimeSettings(), (base) => {
    runtime.value = base
  })
  void api
    .getDetectionRuntime()
    .then((engine) => {
      detectionRuntime.value = engine
    })
    .catch(() => {})
    .finally(() => {
      detectionRuntimePending.value = false
    })
  void loadSection(
    'detection',
    Promise.all([api.listDetectionModels(), api.getDetectionSettings()]),
    ([modelResult, configured]) => {
      models.value = modelResult.items
      track(modelResult.preparation)
      detection.value = configured
    },
  )
  void loadSection(
    'ocr',
    Promise.all([api.listOCRProviders(), api.getOCRSettings()]),
    ([providers, configured]) => {
      ocrProviders.value = providers.items
      ocr.value = configured
    },
  )
  void loadSection(
    'translation',
    Promise.all([api.listLLMProviders(), api.listLLMProfiles(), api.getTranslationSettings()]),
    ([llm, profileResult, configured]) => {
      llmProviders.value = llm.items
      profiles.value = profileResult.items
      translation.value = configured
      if (llmProviders.value[0]) {
        profileForm.value.provider = llmProviders.value[0].id
        profileForm.value.base_url = llmProviders.value[0].default_base_url || ''
      }
    },
  )
  void loadSection(
    'render',
    Promise.all([api.listInpaintingProviders(), api.getRenderSettings(), api.listFonts()]),
    ([providers, configured, fontResult]) => {
      inpaintingProviders.value = providers.items
      renderSettings.value = configured
      fonts.value = fontResult.items
    },
  )
  void loadSection('queue', api.getPerformanceSettings(), (configured) => {
    performance.value = configured
  })
}

const { preparing: modelsPreparing, track } = useDetectionPreparation(async () => {
  const [result, configured] = await Promise.all([
    api.listDetectionModels(),
    api.getDetectionSettings(),
  ])
  models.value = result.items
  detection.value = configured
  track(result.preparation)
})

async function savePerformanceSettings(): Promise<void> {
  if (!performance.value) return
  savingPerformance.value = true
  try {
    performance.value = await api.updatePerformanceSettings({
      ...performance.value,
      expected_revision: performance.value.revision,
    })
    ElMessage.success('任务并发设置已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '任务并发设置保存失败')
  } finally {
    savingPerformance.value = false
  }
}

async function saveRenderSettings(): Promise<void> {
  if (!renderSettings.value) return
  savingRender.value = true
  try {
    renderSettings.value = await api.updateRenderSettings({
      ...renderSettings.value,
      expected_revision: renderSettings.value.revision,
    })
    ElMessage.success('修复与排版默认设置已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '修复设置保存失败')
  } finally {
    savingRender.value = false
  }
}

function chooseFont(): void {
  fontInput.value?.click()
}

async function onFontSelected(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploadingFont.value = true
  try {
    const created = await api.uploadFont(file)
    fonts.value = [...fonts.value.filter((item) => item.id !== created.id), created]
    ElMessage.success(`字体“${created.display_name}”已导入`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '字体导入失败')
  } finally {
    uploadingFont.value = false
  }
}

async function removeFont(font: FontInfo): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除字体“${font.display_name}”？`, '删除字体', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await api.deleteFont(font.id)
    fonts.value = fonts.value.filter((item) => item.id !== font.id)
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error instanceof Error ? error.message : '字体删除失败')
  }
}

function onProfileProviderChange(provider: LLMProfile['provider']): void {
  const info = llmProviders.value.find((item) => item.id === provider)
  if (info?.default_base_url) profileForm.value.base_url = info.default_base_url
}

async function saveProfile(): Promise<void> {
  if (
    !profileForm.value.name.trim() ||
    !profileForm.value.model.trim() ||
    (!editingProfileId.value && !profileForm.value.api_key)
  ) {
    ElMessage.warning(
      editingProfileId.value ? '请填写 Profile 名称和模型' : '请填写 Profile 名称、模型和 API Key',
    )
    return
  }
  savingProfile.value = true
  try {
    if (editingProfileId.value) {
      const payload: Record<string, unknown> = {
        name: profileForm.value.name.trim(),
        provider: profileForm.value.provider,
        base_url: profileForm.value.base_url,
        model: profileForm.value.model.trim(),
        temperature: profileForm.value.temperature,
        max_tokens: profileForm.value.max_tokens,
        timeout_seconds: profileForm.value.timeout_seconds,
        max_concurrency: profileForm.value.max_concurrency,
      }
      if (profileForm.value.api_key) payload.api_key = profileForm.value.api_key
      const updated = await api.updateLLMProfile(editingProfileId.value, payload)
      profiles.value = profiles.value.map((item) => (item.id === updated.id ? updated : item))
      ElMessage.success('翻译 Profile 已更新')
    } else {
      const created = await api.createLLMProfile({
        ...profileForm.value,
        name: profileForm.value.name.trim(),
        model: profileForm.value.model.trim(),
      })
      profiles.value.push(created)
      if (translation.value && !translation.value.default_profile_id) {
        translation.value.default_profile_id = created.id
        await api.updateTranslationSettings(translation.value)
      }
      ElMessage.success('翻译 Profile 已保存，密钥仅保存在 Windows 凭据管理器')
    }
    profileForm.value.api_key = ''
    editingProfileId.value = null
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'Profile 保存失败')
  } finally {
    savingProfile.value = false
  }
}

function editProfile(profile: LLMProfile): void {
  editingProfileId.value = profile.id
  profileForm.value = {
    name: profile.name,
    provider: profile.provider,
    base_url: profile.base_url,
    model: profile.model,
    api_key: '',
    temperature: profile.temperature,
    max_tokens: profile.max_tokens,
    timeout_seconds: profile.timeout_seconds,
    max_concurrency: profile.max_concurrency,
  }
}

function cancelEditProfile(): void {
  editingProfileId.value = null
  profileForm.value.api_key = ''
}

async function testProfile(profile: LLMProfile): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '测试会发送一次最多 64 token 的真实 API 请求，可能产生极小费用。继续吗？',
      '测试连接',
      { confirmButtonText: '继续测试', cancelButtonText: '取消', type: 'warning' },
    )
    const result = await api.testLLMProfile(profile.id)
    ElMessage.success(`连接成功 · ${result.model} · ${result.latency_ms ?? 0} ms`)
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error instanceof Error ? error.message : '连接测试失败')
  }
}

async function removeProfile(profile: LLMProfile): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除 Profile“${profile.name}”？`, '删除 Profile', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await api.deleteLLMProfile(profile.id)
    profiles.value = profiles.value.filter((item) => item.id !== profile.id)
    if (translation.value?.default_profile_id === profile.id) {
      translation.value.default_profile_id = null
      await api.updateTranslationSettings(translation.value)
    }
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error instanceof Error ? error.message : '删除失败')
  }
}

async function saveTranslationSettings(): Promise<void> {
  if (!translation.value) return
  try {
    translation.value = await api.updateTranslationSettings(translation.value)
    ElMessage.success('翻译默认设置已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '翻译设置保存失败')
  }
}

function updateSfxNames(value: string): void {
  if (translation.value)
    translation.value.sfx_class_names = value
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
}

async function saveOCRSettings(): Promise<void> {
  if (!ocr.value) return
  savingOCR.value = true
  try {
    ocr.value = await api.updateOCRSettings(ocr.value)
    ElMessage.success('OCR 默认设置已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'OCR 设置保存失败')
  } finally {
    savingOCR.value = false
  }
}

function chooseModel(): void {
  fileInput.value?.click()
}

async function onModelSelected(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.pt')) {
    ElMessage.warning('请选择 .pt 检测模型')
    return
  }
  uploading.value = true
  uploadProgress.value = 0
  try {
    const created = await api.uploadDetectionModel(
      modelName.value.trim() || file.name.replace(/\.pt$/i, ''),
      file,
      (progress) => {
        if (progress.total)
          uploadProgress.value = Math.round((progress.loaded / progress.total) * 100)
      },
    )
    models.value.push(created)
    if (detection.value && !detection.value.default_model_id)
      detection.value.default_model_id = created.id
    modelName.value = ''
    ElMessage.success(`模型“${created.name}”已验证并保存`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '模型导入失败')
  } finally {
    uploading.value = false
  }
}

async function saveDetectionSettings(): Promise<void> {
  if (!detection.value) return
  saving.value = true
  try {
    detection.value = await api.updateDetectionSettings(detection.value)
    ElMessage.success('检测参数已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function removeModel(model: DetectionModel): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除检测模型“${model.name}”？对应的 ${model.filename} 文件也会从 models/yolo 中永久删除，且无法恢复。`,
      '删除模型',
      {
        confirmButtonText: '永久删除',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    await api.deleteDetectionModel(model.id)
    const [result, configured] = await Promise.all([
      api.listDetectionModels(),
      api.getDetectionSettings(),
    ])
    models.value = result.items
    detection.value = configured
    track(result.preparation)
    ElMessage.success('模型已删除')
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error instanceof Error ? error.message : '删除失败')
  }
}

onMounted(load)
</script>

<template>
  <div class="page-shell settings-page">
    <el-alert
      v-if="modelsPreparing"
      title="检测模型准备中，完成后自动更新"
      type="info"
      :closable="false"
    />
    <section class="page-heading compact-heading">
      <div>
        <h1>设置</h1>
      </div>
    </section>
    <nav class="settings-nav" aria-label="设置分类">
      <a href="#settings-overview">概览</a>
      <a href="#settings-detection">检测</a>
      <a href="#settings-ocr">OCR</a>
      <a href="#settings-translation">翻译</a>
      <a href="#settings-render">成品</a>
      <a href="#settings-queue">任务</a>
    </nav>
    <el-skeleton v-if="pending.overview" id="settings-overview" animated :rows="3" />
    <el-alert
      v-else-if="loadErrors.overview"
      id="settings-overview"
      :title="loadErrors.overview"
      type="error"
      :closable="false"
    />
    <div v-else-if="runtime" id="settings-overview" class="settings-grid">
      <section class="setting-card">
        <el-icon><FolderOpened /></el-icon>
        <div>
          <span>数据工作区</span><strong class="path-value">{{ runtime.data_dir }}</strong
          ><small>原始模型和漫画源文件不会被改写</small>
        </div>
      </section>
      <section class="setting-card">
        <el-icon><DataLine /></el-icon>
        <div>
          <span>预览规格</span
          ><strong>{{ runtime.preview_size }}px / {{ runtime.thumbnail_size }}px</strong
          ><small>页面预览 / 缩略图最长边</small>
        </div>
      </section>
      <section class="setting-card">
        <el-icon><Lock /></el-icon>
        <div>
          <span>访问范围</span><strong>{{ runtime.local_only ? '仅本机' : '局域网' }}</strong
          ><small>服务仅监听 127.0.0.1</small>
        </div>
      </section>
    </div>

    <section id="settings-detection" v-loading="pending.detection" class="detection-settings-panel">
      <el-alert
        v-if="loadErrors.detection"
        :title="loadErrors.detection"
        type="error"
        :closable="false"
      />
      <header class="section-heading-row">
        <div>
          <h2>模型与默认参数</h2>
        </div>
        <el-tag
          v-if="!detectionRuntimePending && !loadErrors.detection"
          :type="runtimeReady ? 'success' : 'warning'"
          effect="plain"
          >{{
            runtimeReady
              ? `推理环境已就绪${detectionRuntime?.cuda_available ? ' · CUDA' : ' · CPU'}`
              : '推理环境未安装'
          }}</el-tag
        >
      </header>
      <p v-if="runtimeReady" class="runtime-device">
        {{ detectionRuntime?.gpu_name || 'CPU'
        }}<template v-if="detectionRuntime?.total_vram_mb">
          · {{ Math.round(detectionRuntime.total_vram_mb / 1024) }} GB 显存</template
        >
        · PyTorch {{ detectionRuntime?.torch_version }} · Ultralytics
        {{ detectionRuntime?.ultralytics_version }}
      </p>
      <div
        v-if="!detectionRuntimePending && !runtimeReady && !loadErrors.detection"
        class="runtime-warning"
      >
        <el-icon><Cpu /></el-icon>
        <div>
          <strong>先安装本地推理依赖</strong
          ><span>运行 scripts/install-detection-gpu.ps1；无 NVIDIA 显卡时可使用 CPU 脚本。</span>
        </div>
      </div>

      <div class="model-grid">
        <article v-for="model in models" :key="model.id" class="model-card">
          <div class="model-card-head">
            <div>
              <strong>{{ model.name }}</strong
              ><small>{{ model.filename }}</small>
            </div>
            <div class="model-card-tags">
              <el-tag :type="model.status === 'ready' ? 'success' : 'warning'" size="small">
                {{
                  model.status === 'ready'
                    ? '已就绪'
                    : model.status === 'preparing'
                      ? '准备中'
                      : '不可用'
                }}
              </el-tag>
              <el-tag v-if="detection?.default_model_id === model.id" type="success" size="small"
                >默认</el-tag
              >
              <el-tag v-if="model.storage_scope === 'project'" type="info" size="small"
                >项目文件</el-tag
              >
            </div>
          </div>
          <dl>
            <div>
              <dt>类别</dt>
              <dd>{{ Object.values(model.class_names).join('、') }}</dd>
            </div>
            <div>
              <dt>Ultralytics</dt>
              <dd>{{ model.framework_version || '未知' }}</dd>
            </div>
            <div>
              <dt>SHA-256</dt>
              <dd class="mono-value">{{ model.sha256.slice(0, 12) }}…</dd>
            </div>
            <div v-if="model.error_message">
              <dt>状态说明</dt>
              <dd>{{ model.error_message }}</dd>
            </div>
          </dl>
          <el-button text type="danger" @click="removeModel(model)">删除模型</el-button>
        </article>
        <button
          type="button"
          class="add-model-card"
          :disabled="uploading || !runtimeReady"
          @click="chooseModel"
        >
          <el-icon><UploadFilled /></el-icon
          ><strong>{{ uploading ? `正在验证 ${uploadProgress}%` : '导入 .pt 模型' }}</strong
          ><span>文件会复制到项目 models/yolo 目录</span>
        </button>
      </div>
      <p v-if="runtime" class="runtime-device path-value">
        YOLO 模型目录：{{ runtime.yolo_models_dir }}；应用会自动扫描其中的 .pt 文件
      </p>

      <input
        ref="fileInput"
        class="visually-hidden"
        type="file"
        accept=".pt"
        @change="onModelSelected"
      />
      <div class="model-import-name">
        <el-input
          v-model="modelName"
          :prefix-icon="Plus"
          placeholder="可选：先填写模型显示名称，再点击导入"
        />
      </div>

      <el-form v-if="detection" class="detection-form" label-position="top">
        <el-form-item label="默认模型"
          ><el-select v-model="detection.default_model_id" clearable placeholder="请选择模型"
            ><el-option
              v-for="model in models"
              :key="model.id"
              :label="detectionModelOptionLabel(model)"
              :value="model.id"
              :disabled="model.status !== 'ready'" /></el-select
        ></el-form-item>
        <el-form-item label="运行设备"
          ><el-select v-model="detection.device"
            ><el-option label="自动（优先 GPU，失败回退 CPU）" value="auto" /><el-option
              label="仅 GPU"
              value="cuda" /><el-option label="仅 CPU" value="cpu" /></el-select
        ></el-form-item>
        <el-form-item label="置信度阈值"
          ><el-input-number
            v-model="detection.confidence"
            :min="0.01"
            :max="1"
            :step="0.05"
            :precision="2"
        /></el-form-item>
        <el-form-item label="输入尺寸"
          ><el-input-number v-model="detection.image_size" :min="320" :max="4096" :step="32"
        /></el-form-item>
        <el-button type="primary" :loading="saving" @click="saveDetectionSettings"
          >保存检测参数</el-button
        >
      </el-form>
    </section>

    <section
      id="settings-ocr"
      v-loading="pending.ocr"
      class="detection-settings-panel ocr-settings-panel"
    >
      <el-alert v-if="loadErrors.ocr" :title="loadErrors.ocr" type="error" :closable="false" />
      <header class="section-heading-row">
        <div>
          <h2>本地 Provider 与固定路由</h2>
        </div>
        <el-tag
          v-if="!pending.ocr && !loadErrors.ocr"
          :type="
            ocrProviders.every((provider) => provider.installed && provider.model_ready)
              ? 'success'
              : 'warning'
          "
          effect="plain"
        >
          {{
            ocrProviders.every((provider) => provider.installed && provider.model_ready)
              ? '全部模型已就绪'
              : '需要显式准备'
          }}
        </el-tag>
      </header>
      <div
        class="runtime-warning"
        v-if="ocrProviders.some((provider) => !provider.installed || !provider.model_ready)"
      >
        <el-icon><Cpu /></el-icon>
        <div>
          <strong>OCR 不会在应用运行时下载依赖或模型</strong>
          <span
            >先运行 scripts/install-ocr-gpu.ps1（或 CPU 脚本），再运行
            scripts/prepare-ocr-models.ps1。</span
          >
        </div>
      </div>
      <div class="model-grid ocr-provider-grid">
        <article v-for="provider in ocrProviders" :key="provider.id" class="model-card">
          <div class="model-card-head">
            <div>
              <strong>{{ provider.name }}</strong
              ><small>{{ provider.supported_languages.join(' / ') }}</small>
            </div>
            <el-tag
              :type="provider.installed && provider.model_ready ? 'success' : 'warning'"
              size="small"
            >
              {{
                !provider.installed ? '依赖未安装' : provider.model_ready ? '已就绪' : '模型未准备'
              }}
            </el-tag>
          </div>
          <dl>
            <div>
              <dt>包版本</dt>
              <dd>{{ provider.version || '未安装' }}</dd>
            </div>
            <div>
              <dt>模型</dt>
              <dd>{{ provider.model_names.join('、') }}</dd>
            </div>
            <div>
              <dt>可用设备</dt>
              <dd>{{ provider.cuda_available ? 'CUDA / CPU' : 'CPU' }}</dd>
            </div>
          </dl>
        </article>
      </div>
      <p v-if="runtime" class="runtime-device path-value">
        项目 OCR 模型：{{ runtime.ocr_models_dir }}
      </p>
      <el-form v-if="ocr" class="ocr-form" label-position="top">
        <el-form-item label="日文 OCR">
          <el-input value="MangaOCR（固定）" disabled />
        </el-form-item>
        <el-form-item label="韩文 OCR">
          <el-input value="PaddleOCR（固定）" disabled />
        </el-form-item>
        <el-form-item label="英文 OCR">
          <el-input value="PaddleOCR（固定）" disabled />
        </el-form-item>
        <el-form-item label="运行设备">
          <el-select v-model="ocr.device"
            ><el-option label="自动（GPU 失败回退 CPU）" value="auto" /><el-option
              label="仅 GPU"
              value="cuda" /><el-option label="仅 CPU" value="cpu"
          /></el-select>
        </el-form-item>
        <el-button type="primary" :loading="savingOCR" @click="saveOCRSettings"
          >保存运行设备</el-button
        >
      </el-form>
    </section>

    <section
      id="settings-translation"
      v-loading="pending.translation"
      class="detection-settings-panel translation-settings-panel"
    >
      <el-alert
        v-if="loadErrors.translation"
        :title="loadErrors.translation"
        type="error"
        :closable="false"
      />
      <header class="section-heading-row">
        <div>
          <h2>翻译模型与 API</h2>
        </div>
        <el-tag type="info" effect="plain">只发送文本，不上传漫画图片</el-tag>
      </header>
      <div class="model-grid ocr-provider-grid">
        <article v-for="profile in profiles" :key="profile.id" class="model-card">
          <div class="model-card-head">
            <div>
              <strong>{{ profile.name }}</strong
              ><small>{{ profile.provider }} · {{ profile.model }}</small>
            </div>
            <el-tag
              v-if="translation?.default_profile_id === profile.id"
              type="success"
              size="small"
              >默认</el-tag
            >
          </div>
          <dl>
            <div>
              <dt>地址</dt>
              <dd class="path-value">{{ profile.base_url }}</dd>
            </div>
            <div>
              <dt>密钥</dt>
              <dd>{{ profile.has_api_key ? profile.api_key_hint : '未配置' }}</dd>
            </div>
            <div>
              <dt>参数</dt>
              <dd>
                {{
                  profile.provider === 'deepseek'
                    ? 'Token 跟随服务端'
                    : `${profile.max_tokens} tokens`
                }}
                / {{ profile.timeout_seconds }} 秒
              </dd>
            </div>
          </dl>
          <div class="profile-actions">
            <el-button size="small" @click="testProfile(profile)">测试连接</el-button>
            <el-button size="small" @click="editProfile(profile)">编辑</el-button>
            <el-button size="small" type="danger" text @click="removeProfile(profile)"
              >删除</el-button
            >
          </div>
        </article>
      </div>
      <el-form class="detection-form profile-form" label-position="top">
        <el-form-item label="Profile 名称"
          ><el-input v-model="profileForm.name" placeholder="例如：我的 DeepSeek"
        /></el-form-item>
        <el-form-item label="Provider"
          ><el-select v-model="profileForm.provider" @change="onProfileProviderChange"
            ><el-option
              v-for="provider in llmProviders"
              :key="provider.id"
              :label="provider.name"
              :value="provider.id" /></el-select
        ></el-form-item>
        <el-form-item label="Base URL"
          ><el-input v-model="profileForm.base_url" placeholder="远程地址必须 HTTPS"
        /></el-form-item>
        <el-form-item label="模型"
          ><el-input v-model="profileForm.model" placeholder="例如 gpt-4o-mini"
        /></el-form-item>
        <el-form-item label="API Key"
          ><el-input
            v-model="profileForm.api_key"
            type="password"
            show-password
            autocomplete="new-password"
          />
          <small class="field-note">密钥保存在本机凭据管理器，界面只显示末四位。</small>
        </el-form-item>
        <el-form-item v-if="profileForm.provider !== 'deepseek'" label="最大 Token"
          ><el-input-number v-model="profileForm.max_tokens" :min="64" :max="32768"
        /></el-form-item>
        <el-form-item v-else label="最大 Token">
          <span>不设置本地上限，跟随 DeepSeek 服务端默认额度</span>
        </el-form-item>
        <el-form-item label="Temperature（可选）"
          ><el-input-number
            v-model="profileForm.temperature"
            :min="0"
            :max="2"
            :step="0.1"
            :precision="2"
            clearable
        /></el-form-item>
        <el-form-item label="超时（秒）"
          ><el-input-number v-model="profileForm.timeout_seconds" :min="5" :max="600"
        /></el-form-item>
        <el-button type="primary" :loading="savingProfile" @click="saveProfile">{{
          editingProfileId ? '保存 Profile' : '添加 Profile'
        }}</el-button>
        <el-button v-if="editingProfileId" @click="cancelEditProfile">取消编辑</el-button>
      </el-form>
      <el-form
        v-if="translation"
        class="detection-form translation-default-form"
        label-position="top"
      >
        <el-form-item label="默认 Profile"
          ><el-select v-model="translation.default_profile_id" clearable placeholder="不设置"
            ><el-option
              v-for="profile in profiles"
              :key="profile.id"
              :label="profile.name"
              :value="profile.id" /></el-select
        ></el-form-item>
        <el-form-item label="SFX 策略"
          ><el-select v-model="translation.sfx_strategy"
            ><el-option label="保留原文（本地完成）" value="preserve" /><el-option
              label="替换"
              value="replace" /><el-option label="双语" value="bilingual" /></el-select
        ></el-form-item>
        <el-form-item label="SFX 类别（逗号分隔）"
          ><el-input
            :model-value="translation.sfx_class_names.join(',')"
            @update:model-value="updateSfxNames"
        /></el-form-item>
        <el-button type="primary" @click="saveTranslationSettings">保存翻译设置</el-button>
      </el-form>
    </section>
    <section
      id="settings-render"
      v-loading="pending.render"
      class="detection-settings-panel translation-settings-panel"
    >
      <el-alert
        v-if="loadErrors.render"
        :title="loadErrors.render"
        type="error"
        :closable="false"
      />
      <header class="section-heading-row">
        <div>
          <h2>当前页成品生成</h2>
        </div>
        <el-tag
          v-if="!pending.render && !loadErrors.render"
          :type="
            inpaintingProviders.some((provider) => provider.id === 'lama' && provider.model_ready)
              ? 'success'
              : 'warning'
          "
          effect="plain"
        >
          {{
            inpaintingProviders.some((provider) => provider.id === 'lama' && provider.model_ready)
              ? 'Big-LaMa 已就绪'
              : '仅 FAST / OpenCV'
          }}
        </el-tag>
      </header>
      <div class="model-grid ocr-provider-grid">
        <article v-for="provider in inpaintingProviders" :key="provider.id" class="model-card">
          <div class="model-card-head">
            <div>
              <strong>{{ provider.name }}</strong
              ><small>{{ provider.description }}</small>
            </div>
            <el-tag
              :type="provider.model_ready || provider.id !== 'lama' ? 'success' : 'warning'"
              size="small"
            >
              {{
                provider.id === 'lama'
                  ? provider.model_ready
                    ? '模型已准备'
                    : '模型未准备'
                  : provider.installed
                    ? '可用'
                    : '依赖未安装'
              }}
            </el-tag>
          </div>
          <dl>
            <div>
              <dt>版本</dt>
              <dd>
                {{
                  provider.version ||
                  (provider.id === 'auto' || provider.id === 'fast' ? '内置算法' : '未安装')
                }}
              </dd>
            </div>
            <div>
              <dt>设备</dt>
              <dd>{{ provider.available_devices.join(' / ') || 'CPU' }}</dd>
            </div>
          </dl>
        </article>
      </div>
      <p v-if="runtime" class="runtime-device path-value">
        Big-LaMa 项目位置：{{
          runtime.inpainting_models_dir
        }}\big-lama.pt；准备脚本：scripts/prepare-inpainting-model.ps1
      </p>
      <el-form v-if="renderSettings" class="detection-form" label-position="top">
        <el-form-item label="默认修复模式"
          ><el-select v-model="renderSettings.repair_mode"
            ><el-option label="Auto（按背景复杂度）" value="auto" /><el-option
              label="FAST"
              value="fast" /><el-option label="OpenCV Telea" value="opencv" /><el-option
              label="Big-LaMa"
              value="lama" /></el-select
        ></el-form-item>
        <el-form-item label="运行设备"
          ><el-select v-model="renderSettings.device"
            ><el-option label="自动（CUDA 失败回退 CPU）" value="auto" /><el-option
              label="仅 CPU"
              value="cpu" /><el-option label="仅 CUDA" value="cuda" /></el-select
        ></el-form-item>
        <el-form-item label="文字搜索外扩比例"
          ><el-input-number
            v-model="renderSettings.mask_padding_ratio"
            :min="0"
            :max="0.25"
            :step="0.01"
            :precision="2"
        /></el-form-item>
        <el-form-item label="文字笔画膨胀像素"
          ><el-input-number v-model="renderSettings.mask_dilation_px" :min="0" :max="2"
        /></el-form-item>
        <p>
          纯色背景自动提取文字笔画并保护周边线条，膨胀最多 2 像素；复杂背景回退到不外扩的文字框。
        </p>
        <el-form-item label="OpenCV 修复半径"
          ><el-input-number v-model="renderSettings.opencv_radius" :min="0.1" :max="20" :step="0.5"
        /></el-form-item>
        <el-form-item label="Big-LaMa 最大边"
          ><el-input-number v-model="renderSettings.lama_max_edge" :min="512" :max="8192"
        /></el-form-item>
        <el-form-item label="AI 修复失败时回退 OpenCV"
          ><el-switch v-model="renderSettings.ai_fallback"
        /></el-form-item>
        <el-form-item label="默认字体"
          ><el-select v-model="renderSettings.font_id"
            ><el-option
              v-for="font in fonts"
              :key="font.id"
              :label="font.display_name"
              :value="font.id"
              :disabled="font.status !== 'ready'" /></el-select
        ></el-form-item>
        <el-form-item label="默认字号"
          ><el-input-number v-model="renderSettings.font_size" :min="6" :max="512"
        /></el-form-item>
        <el-form-item label="自动字号"
          ><el-switch v-model="renderSettings.auto_font_size"
        /></el-form-item>
        <el-form-item label="文字方向"
          ><el-select v-model="renderSettings.orientation"
            ><el-option label="自动" value="auto" /><el-option
              label="横排"
              value="horizontal" /><el-option label="竖排" value="vertical" /></el-select
        ></el-form-item>
        <el-form-item label="最小字号"
          ><el-input-number v-model="renderSettings.min_font_size" :min="6" :max="512"
        /></el-form-item>
        <el-form-item label="最大字号"
          ><el-input-number v-model="renderSettings.max_font_size" :min="6" :max="512"
        /></el-form-item>
        <el-form-item label="文字边距比例"
          ><el-input-number
            v-model="renderSettings.margin_ratio"
            :min="0"
            :max="0.45"
            :step="0.01"
            :precision="2"
        /></el-form-item>
        <el-form-item label="描边宽度"
          ><el-input-number v-model="renderSettings.stroke_width" :min="0" :max="64"
        /></el-form-item>
        <el-form-item label="文字颜色"
          ><el-input v-model="renderSettings.font_color" placeholder="#000000"
        /></el-form-item>
        <el-form-item label="描边颜色"
          ><el-input v-model="renderSettings.stroke_color" placeholder="#FFFFFF"
        /></el-form-item>
        <el-form-item label="旋转角度"
          ><el-input-number v-model="renderSettings.rotation_degrees" :min="-180" :max="180"
        /></el-form-item>
        <el-button type="primary" :loading="savingRender" @click="saveRenderSettings"
          >保存修复与排版设置</el-button
        >
      </el-form>
      <div class="model-grid ocr-provider-grid font-grid">
        <article v-for="font in fonts" :key="font.id" class="model-card">
          <div class="model-card-head">
            <div>
              <strong>{{ font.display_name }}</strong
              ><small>{{ font.filename }}</small>
            </div>
            <el-tag size="small" :type="font.status === 'ready' ? 'success' : 'warning'">{{
              font.status === 'ready' ? '可用' : '不可用'
            }}</el-tag>
          </div>
          <dl>
            <div>
              <dt>来源</dt>
              <dd>{{ font.source === 'system' ? '系统字体' : '自定义字体' }}</dd>
            </div>
            <div>
              <dt>大小</dt>
              <dd>{{ font.size ? `${Math.round(font.size / 1024)} KB` : '—' }}</dd>
            </div>
          </dl>
          <el-button v-if="font.deletable" text type="danger" @click="removeFont(font)"
            >删除</el-button
          >
        </article>
        <button type="button" class="add-model-card" :disabled="uploadingFont" @click="chooseFont">
          <el-icon><UploadFilled /></el-icon
          ><strong>{{ uploadingFont ? '正在导入…' : '导入 TTF / OTF / TTC' }}</strong
          ><span>最大 50 MiB，仅保存字体元数据与工作区文件</span>
        </button>
      </div>
      <input
        ref="fontInput"
        class="visually-hidden"
        type="file"
        accept=".ttf,.otf,.ttc"
        @change="onFontSelected"
      />
    </section>
    <section
      id="settings-queue"
      v-loading="pending.queue"
      class="detection-settings-panel translation-settings-panel performance-settings-panel"
    >
      <el-alert v-if="loadErrors.queue" :title="loadErrors.queue" type="error" :closable="false" />
      <header class="section-heading-row">
        <div>
          <h2>任务并发</h2>
        </div>
        <el-tag type="info" effect="plain">活动任务期间锁定</el-tag>
      </header>
      <el-form v-if="performance" class="detection-form performance-form" label-position="top">
        <el-form-item label="批处理并行项目数">
          <el-input-number v-model="performance.batch_concurrency" :min="1" :max="8" />
        </el-form-item>
        <el-form-item label="流水线窗口（页）">
          <el-input-number v-model="performance.pipeline_window" :min="1" :max="32" />
        </el-form-item>
        <el-form-item label="OCR 并发">
          <el-input-number v-model="performance.ocr_concurrency" :min="1" :max="8" />
        </el-form-item>
        <el-form-item label="LLM 并发">
          <el-input-number v-model="performance.llm_concurrency" :min="1" :max="16" />
        </el-form-item>
        <div class="fixed-concurrency-note">
          固定限制：YOLO {{ performance.yolo_concurrency }} · Big-LaMa
          {{ performance.lama_concurrency }} · 渲染 {{ performance.render_concurrency }}
        </div>
        <el-button type="primary" :loading="savingPerformance" @click="savePerformanceSettings">
          保存任务并发设置
        </el-button>
      </el-form>
    </section>
    <div v-if="runtime" class="version-line">漫画智能翻译 · Phase 9 · v{{ runtime.version }}</div>
  </div>
</template>
