<script setup lang="ts">
import { FolderOpened, Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { RecycleScroller } from 'vue-virtual-scroller'

import { api } from '@/api/client'
import ProjectCard from '@/components/ProjectCard.vue'
import { preloadProjectEditor } from '@/router'
import { useProjectsStore } from '@/stores/projects'
import type { ProjectSummary } from '@/types'

const router = useRouter()
const store = useProjectsStore()
const loading = ref(!store.loaded)
const loadingMore = ref(false)
const refreshing = ref(false)

async function load(reset = false): Promise<void> {
  if (refreshing.value || loadingMore.value) return
  if (reset) {
    refreshing.value = true
    loading.value = !store.loaded
  } else {
    loadingMore.value = true
  }
  try {
    const page = await api.listProjects(reset ? 0 : store.items.length, 30)
    if (reset) {
      const sameFirstPage = page.items.every((item, index) => store.items[index]?.id === item.id)
      const retained = sameFirstPage ? store.items.slice(page.items.length) : []
      store.replace([...page.items, ...retained].slice(0, page.total), page.total)
    } else store.append(page.items, page.total)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '项目加载失败')
  } finally {
    loading.value = false
    loadingMore.value = false
    refreshing.value = false
  }
}

function onVisibleRange(_start: number, end: number): void {
  if (
    end >= store.items.length - 5 &&
    store.items.length < store.total &&
    !loadingMore.value &&
    !refreshing.value
  )
    load()
}

function openProject(project: ProjectSummary): void {
  router.push({
    name: 'project',
    params: { id: project.id },
  })
}

async function remove(project: ProjectSummary): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `“${project.name}”的工作区副本、缩略图和项目记录将被永久删除。用户原始文件不会受影响。`,
      '删除项目？',
      { confirmButtonText: '永久删除', cancelButtonText: '保留项目', type: 'warning' },
    )
    await api.deleteProject(project.id)
    store.remove(project.id)
    ElMessage.success('项目已删除')
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '项目删除失败')
  }
}

onMounted(() => {
  void load(true)
  preloadProjectEditor()
})
</script>

<template>
  <div class="page-shell projects-page">
    <section class="page-heading">
      <div>
        <h1>翻译项目</h1>
      </div>
      <el-button type="primary" size="large" :icon="Plus" @click="router.push('/projects/new')">
        新建项目
      </el-button>
    </section>

    <div v-if="loading" class="skeleton-list">
      <el-skeleton v-for="index in 3" :key="index" animated :rows="3" />
    </div>

    <section v-else-if="!store.items.length" class="empty-workspace small-empty">
      <div class="empty-icon">
        <el-icon><FolderOpened /></el-icon>
      </div>
      <h2>还没有翻译项目</h2>
      <el-button type="primary" @click="router.push('/projects/new')">新建项目</el-button>
    </section>

    <RecycleScroller
      v-else
      class="virtual-project-list"
      :items="store.items"
      :item-size="190"
      key-field="id"
      @update="onVisibleRange"
    >
      <template #default="{ item }">
        <ProjectCard :project="item" @open="openProject(item)" @delete="remove(item)" />
      </template>
    </RecycleScroller>
  </div>
</template>
