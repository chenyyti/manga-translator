<script setup lang="ts">
import { ArrowLeft, ArrowRight, Back, FullScreen, Loading, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '@/api/client'
import { BlobLruCache } from '@/composables/useBlobLru'
import type { ReaderManifest, ReaderPage } from '@/types'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const manifest = ref<ReaderManifest | null>(null)
const pages = ref<ReaderPage[]>([])
const loadedOffsets = new Set<number>()
const pendingChunks = new Map<number, Promise<void>>()
const currentPageIndex = ref(1)
const pageInput = ref(1)
const loading = ref(true)
const loadingPage = ref(false)
const loadError = ref('')
const assetUrl = ref<string | null>(null)
const isFullscreen = ref(false)
const readerElement = ref<HTMLElement | null>(null)
const blobCache = new BlobLruCache(8)
let assetRequest = 0
let activeAssetKey: string | null = null
let syncingRoute = false
let metadataEpoch = 0
let readerEpoch = 0

const totalPages = computed(() => manifest.value?.total_pages ?? 0)
const currentPage = computed(
  () => pages.value.find((page) => page.page_index === currentPageIndex.value) ?? null,
)
const canPrevious = computed(() => currentPageIndex.value > 1)
const canNext = computed(() => currentPageIndex.value < totalPages.value)
const missingReasonLabels: Record<string, string> = {
  source_invalid: '原图损坏或无法读取',
  render_outdated: '成品已过期，请回到项目页重新生成',
  render_failed: '成品生成失败，请回到项目页重试',
  render_missing: '该页尚未生成成品',
}

async function loadChunk(offset: number): Promise<void> {
  if (offset < 0 || loadedOffsets.has(offset)) return
  const pending = pendingChunks.get(offset)
  if (pending) return pending
  const requestEpoch = metadataEpoch
  const operation = api
    .listReaderPages(projectId.value, offset, 50)
    .then((result) => {
      if (requestEpoch !== metadataEpoch) return
      loadedOffsets.add(offset)
      const byId = new Map(pages.value.map((page) => [page.id, page]))
      for (const page of result.items) byId.set(page.id, page)
      pages.value = [...byId.values()].sort((left, right) => left.page_index - right.page_index)
    })
    .finally(() => {
      if (pendingChunks.get(offset) === operation) pendingChunks.delete(offset)
    })
  pendingChunks.set(offset, operation)
  return operation
}

async function ensurePage(pageIndex: number): Promise<ReaderPage | null> {
  if (!totalPages.value || pageIndex < 1 || pageIndex > totalPages.value) return null
  await loadChunk(Math.floor((pageIndex - 1) / 50) * 50)
  return pages.value.find((page) => page.page_index === pageIndex) ?? null
}

async function loadAsset(page: ReaderPage | null): Promise<void> {
  const requestId = ++assetRequest
  if (activeAssetKey) blobCache.delete(activeAssetKey)
  activeAssetKey = null
  assetUrl.value = null
  if (!page?.readable || !page.rendered_url) return
  loadingPage.value = true
  try {
    const key = `reader:${page.id}`
    const url = await blobCache.get(key, () => api.getPreviewBlob(page.rendered_url!))
    if (requestId !== assetRequest) return
    activeAssetKey = key
    assetUrl.value = url
    void prefetchAround(page.page_index, requestId)
  } catch (error) {
    if (requestId === assetRequest) {
      ElMessage.error(error instanceof Error ? error.message : '成品加载失败')
    }
  } finally {
    if (requestId === assetRequest) loadingPage.value = false
  }
}

async function prefetchAround(pageIndex: number, requestId: number): Promise<void> {
  for (const distance of [-2, -1, 1, 2]) {
    if (requestId !== assetRequest) return
    const page = await ensurePage(pageIndex + distance)
    if (!page?.readable || !page.rendered_url) continue
    const key = `reader:${page.id}`
    await blobCache.get(key, () => api.getPreviewBlob(page.rendered_url!)).catch(() => undefined)
  }
}

async function selectPage(
  pageIndex: number,
  updateUrl = true,
  expectedReaderEpoch = readerEpoch,
): Promise<void> {
  if (expectedReaderEpoch !== readerEpoch) return
  const target = await ensurePage(pageIndex)
  if (!target || expectedReaderEpoch !== readerEpoch) return
  currentPageIndex.value = target.page_index
  pageInput.value = target.page_index
  await loadAsset(target)
  if (expectedReaderEpoch !== readerEpoch) return
  if (updateUrl && !syncingRoute) {
    syncingRoute = true
    try {
      await router.replace({ query: { page: String(target.page_index) } })
    } finally {
      syncingRoute = false
    }
  }
  void api.updateReadingProgress(projectId.value, target.id).catch((error) => {
    ElMessage.warning(error instanceof Error ? error.message : '阅读进度保存失败')
  })
}

async function movePage(delta: number): Promise<void> {
  const target = currentPageIndex.value + delta
  if (target >= 1 && target <= totalPages.value) await selectPage(target)
}

async function jumpToPage(): Promise<void> {
  const value = Number(pageInput.value)
  if (!Number.isInteger(value) || value < 1 || value > totalPages.value) {
    pageInput.value = currentPageIndex.value
    ElMessage.warning(`请输入 1-${totalPages.value} 之间的页码`)
    return
  }
  await selectPage(value)
}

async function loadReader(): Promise<void> {
  const requestEpoch = ++readerEpoch
  loading.value = true
  loadError.value = ''
  assetRequest += 1
  blobCache.clear()
  activeAssetKey = null
  assetUrl.value = null
  // A refresh must not reuse stale page metadata after a render task or an
  // external file cleanup changed readability.  In-flight requests are still
  // generation-guarded by their cache epoch and cannot leak object URLs.
  metadataEpoch += 1
  loadedOffsets.clear()
  pendingChunks.clear()
  pages.value = []
  try {
    const nextManifest = await api.getReaderManifest(projectId.value)
    if (requestEpoch !== readerEpoch) return
    manifest.value = nextManifest
    const queryPage = Number(route.query.page)
    const initial =
      Number.isInteger(queryPage) && queryPage >= 1
        ? queryPage
        : (manifest.value.last_read_page_index ?? manifest.value.first_page_index ?? 1)
    await selectPage(
      Math.min(Math.max(initial, 1), Math.max(totalPages.value, 1)),
      true,
      requestEpoch,
    )
  } catch (error) {
    if (requestEpoch === readerEpoch)
      loadError.value = error instanceof Error ? error.message : '阅读器加载失败'
  } finally {
    if (requestEpoch === readerEpoch) loading.value = false
  }
}

async function toggleFullscreen(): Promise<void> {
  if (!readerElement.value) return
  try {
    if (document.fullscreenElement) await document.exitFullscreen()
    else if (readerElement.value.requestFullscreen) await readerElement.value.requestFullscreen()
    else throw new Error('当前浏览器不支持全屏')
  } catch (error) {
    ElMessage.warning(error instanceof Error ? error.message : '无法切换全屏')
  }
}

function syncFullscreen(): void {
  isFullscreen.value = document.fullscreenElement === readerElement.value
}

function onKeydown(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null
  if (target?.matches('input, textarea, select, [contenteditable="true"]')) return
  if (event.key === 'ArrowLeft') {
    event.preventDefault()
    void movePage(-1)
  } else if (event.key === 'ArrowRight') {
    event.preventDefault()
    void movePage(1)
  } else if (event.key === 'Home') {
    event.preventDefault()
    void selectPage(1)
  } else if (event.key === 'End') {
    event.preventDefault()
    void selectPage(totalPages.value)
  } else if (event.key === 'Escape' && document.fullscreenElement) {
    void document.exitFullscreen()
  }
}

watch(
  () => route.query.page,
  (value) => {
    if (syncingRoute) return
    const pageIndex = Number(value)
    if (Number.isInteger(pageIndex) && pageIndex !== currentPageIndex.value) {
      void selectPage(pageIndex, false)
    }
  },
)

watch(projectId, (value, previous) => {
  if (value === previous) return
  // Vue Router reuses the reader component when only :projectId changes.
  // Treat that as a full reader switch so old metadata and Blob URLs cannot
  // bleed into the next project.
  assetRequest += 1
  blobCache.clear()
  activeAssetKey = null
  assetUrl.value = null
  manifest.value = null
  void loadReader()
})

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  document.addEventListener('fullscreenchange', syncFullscreen)
  void loadReader()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  document.removeEventListener('fullscreenchange', syncFullscreen)
  readerEpoch += 1
  metadataEpoch += 1
  assetRequest += 1
  blobCache.clear()
  activeAssetKey = null
  assetUrl.value = null
})
</script>

<template>
  <section ref="readerElement" class="reader-page">
    <header class="reader-toolbar">
      <el-button :icon="Back" text @click="router.push('/')">返回书架</el-button>
      <div class="reader-title">
        <strong>{{ manifest?.project_name ?? '正在加载' }}</strong>
      </div>
      <div class="reader-toolbar-actions">
        <el-button :icon="Refresh" text :loading="loading" @click="loadReader">刷新</el-button>
        <el-button :icon="FullScreen" text @click="toggleFullscreen">
          {{ isFullscreen ? '退出全屏' : '全屏' }}
        </el-button>
      </div>
    </header>

    <div v-if="loading" class="reader-state">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>正在打开成品…</span>
    </div>
    <div v-else-if="loadError" class="reader-state reader-error">
      <h2>暂时无法打开阅读器</h2>
      <p>{{ loadError }}</p>
      <el-button type="primary" @click="loadReader">重试</el-button>
    </div>
    <template v-else>
      <main class="reader-stage" :class="{ 'reader-stage-fullscreen': isFullscreen }">
        <div v-if="!currentPage?.readable" class="reader-missing">
          <span class="missing-page-number">第 {{ currentPageIndex }} 页</span>
          <h2>该页暂不可阅读</h2>
          <p>{{ missingReasonLabels[currentPage?.missing_reason ?? ''] ?? '成品不存在' }}</p>
        </div>
        <div v-else-if="assetUrl" class="reader-image-wrap">
          <img :src="assetUrl" :alt="`${manifest?.project_name} 第 ${currentPageIndex} 页`" />
        </div>
        <div v-else class="reader-state reader-inline-loading">
          <el-icon class="is-loading"><Loading /></el-icon>
          <span>{{ loadingPage ? '正在载入页面…' : '成品尚未生成' }}</span>
        </div>
      </main>

      <footer class="reader-controls">
        <el-button :icon="ArrowLeft" :disabled="!canPrevious" @click="movePage(-1)">
          上一页
        </el-button>
        <div class="reader-page-jump">
          <el-input-number
            v-model="pageInput"
            :min="1"
            :max="totalPages"
            controls-position="right"
            aria-label="页码"
            @change="jumpToPage"
          />
          <span>/ {{ totalPages }}</span>
        </div>
        <el-button :icon="ArrowRight" :disabled="!canNext" @click="movePage(1)"> 下一页 </el-button>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.reader-page {
  min-height: calc(100vh - 72px);
  display: flex;
  flex-direction: column;
  background: #151719;
  color: #f7f8f8;
}

.reader-toolbar {
  min-height: 68px;
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 0 24px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.reader-toolbar :deep(.el-button) {
  color: #f7f8f8;
}

.reader-title {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.reader-toolbar-actions {
  display: flex;
  gap: 4px;
}

.reader-stage {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 32px;
}

.reader-stage-fullscreen {
  background: #151719;
}

.reader-image-wrap {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.reader-image-wrap img {
  max-width: 100%;
  max-height: calc(100vh - 180px);
  object-fit: contain;
  box-shadow: 0 18px 70px rgba(0, 0, 0, 0.45);
}

.reader-missing {
  width: min(480px, 90vw);
  padding: 48px 32px;
  text-align: center;
  border: 1px dashed rgba(255, 255, 255, 0.22);
  border-radius: 18px;
  color: #d9dfdf;
}

.missing-page-number {
  color: #f0b45a;
  font-size: 13px;
  letter-spacing: 0.12em;
}

.reader-missing h2 {
  margin: 16px 0 8px;
  color: #fff;
}

.reader-missing p,
.reader-error p {
  color: #aab3b3;
}

.reader-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  flex-direction: column;
  color: #aab3b3;
}

.reader-error {
  text-align: center;
}

.reader-error h2 {
  color: #fff;
}

.reader-inline-loading {
  flex: none;
  min-height: 260px;
}

.reader-controls {
  min-height: 78px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 22px;
  padding: 0 24px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
}

.reader-controls :deep(.el-button) {
  color: #fff;
}

.reader-page-jump {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #bbc4c4;
}

.reader-page-jump :deep(.el-input-number) {
  width: 116px;
}

@media (max-width: 700px) {
  .reader-toolbar {
    padding: 0 12px;
    gap: 8px;
  }

  .reader-toolbar-actions .el-button:first-child {
    display: none;
  }

  .reader-stage {
    padding: 12px;
  }

  .reader-controls {
    gap: 8px;
  }
}
</style>
