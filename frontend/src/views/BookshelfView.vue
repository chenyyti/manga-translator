<script setup lang="ts">
import { Reading } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { storeToRefs } from 'pinia'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { RecycleScroller } from 'vue-virtual-scroller'

import { api } from '@/api/client'
import ProjectCard from '@/components/ProjectCard.vue'
import { useBookshelfStore } from '@/stores/bookshelf'

const router = useRouter()
const store = useBookshelfStore()
const { items, total } = storeToRefs(store)
const loading = ref(!store.loaded)
const loadingMore = ref(false)
const refreshing = ref(false)
const gridWidth = ref(1180)
const gridElement = ref<HTMLElement>()
let gridObserver: ResizeObserver | null = null
const gridColumns = computed(() => {
  if (gridWidth.value >= 1100) return 5
  if (gridWidth.value >= 850) return 4
  if (gridWidth.value >= 620) return 3
  return 2
})
const gridItemWidth = computed(() => Math.floor(gridWidth.value / gridColumns.value))
const gridItemHeight = computed(() => Math.round((gridItemWidth.value - 16) * 1.28 + 150))

async function load(reset = false): Promise<void> {
  if (refreshing.value || loadingMore.value) return
  if (reset) {
    refreshing.value = true
    loading.value = !store.loaded
  } else {
    loadingMore.value = true
  }
  try {
    const page = await api.listBookshelf(reset ? 0 : items.value.length, 30)
    if (reset) {
      const sameFirstPage = page.items.every((item, index) => items.value[index]?.id === item.id)
      const retained = sameFirstPage ? items.value.slice(page.items.length) : []
      store.replace([...page.items, ...retained].slice(0, page.total), page.total)
    } else {
      store.append(page.items, page.total)
    }
    if (reset) {
      loading.value = false
      await nextTick()
      performance.mark('bookshelf-ready')
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '书架加载失败')
  } finally {
    loading.value = false
    loadingMore.value = false
    refreshing.value = false
  }
}

function onVisibleRange(_start: number, end: number): void {
  if (
    end >= items.value.length - 5 &&
    items.value.length < total.value &&
    !loadingMore.value &&
    !refreshing.value
  )
    load()
}

watch(
  gridElement,
  (element) => {
    gridObserver?.disconnect()
    if (!element) return
    gridObserver = new ResizeObserver(([entry]) => {
      gridWidth.value = entry.contentRect.width
    })
    gridObserver.observe(element)
  },
  { flush: 'post' },
)
onMounted(() => {
  void load(true)
})
onBeforeUnmount(() => gridObserver?.disconnect())
</script>

<template>
  <div class="page-shell bookshelf-page">
    <section class="page-heading compact-heading">
      <div>
        <h1>我的书架</h1>
      </div>
      <div v-if="items.length" class="heading-count">{{ total }} 本可阅读漫画</div>
    </section>

    <div v-if="loading" class="skeleton-list" aria-label="正在加载书架">
      <el-skeleton v-for="index in 3" :key="index" animated :rows="3" />
    </div>

    <section v-else-if="!items.length" class="empty-workspace">
      <div class="empty-icon">
        <el-icon><Reading /></el-icon>
      </div>
      <h2>书架为空</h2>
      <el-button type="primary" size="large" @click="router.push('/projects/new')"
        >新建项目</el-button
      >
      <button class="text-link" type="button" @click="router.push('/projects')">
        查看翻译项目
      </button>
    </section>

    <div v-else ref="gridElement" class="bookshelf-grid-wrap">
      <RecycleScroller
        class="virtual-project-list bookshelf-grid"
        :items="items"
        :item-size="gridItemHeight"
        :item-secondary-size="gridItemWidth"
        :grid-items="gridColumns"
        key-field="id"
        @update="onVisibleRange"
      >
        <template #default="{ item }">
          <ProjectCard
            :project="item"
            reader-mode
            @read="router.push(item.continue_url ?? `/reader/${item.id}?page=1`)"
          />
        </template>
      </RecycleScroller>
    </div>
  </div>
</template>
