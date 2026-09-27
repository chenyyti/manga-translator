<script setup lang="ts">
import { RefreshRight, VideoPause, VideoPlay, Close } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RecycleScroller } from 'vue-virtual-scroller'

import { api } from '@/api/client'
import type { TaskItemRead, TaskSnapshot } from '@/types'

const tasks = ref<TaskSnapshot[]>([])
const loading = ref(false)
const projectFilter = ref('')
const statusFilter = ref('')
const typeFilter = ref('')
const selected = ref<TaskSnapshot | null>(null)
const items = ref<TaskItemRead[]>([])
const drawerVisible = ref(false)
let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false

const filtered = computed(() =>
  tasks.value.filter(
    (task) =>
      (!projectFilter.value.trim() || task.project_id === projectFilter.value.trim()) &&
      (!statusFilter.value || task.status === statusFilter.value) &&
      (!typeFilter.value || task.task_type === typeFilter.value),
  ),
)
const statusLabels: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  pausing: '暂停中',
  paused: '已暂停',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}
const typeLabels: Record<string, string> = {
  project_import: '项目导入',
  batch_pipeline: '批处理',
  page_detection: '批量检测',
  ocr: 'OCR',
  quick_translation: '翻译',
  chapter_summary: '章节摘要',
  page_repair: '页面修复',
  page_render: '页面生成',
  export_zip: 'ZIP 导出',
  export_epub: 'EPUB 导出',
}

async function loadTasks(): Promise<void> {
  loading.value = true
  try {
    const result = await api.listTasks({
      project_id: projectFilter.value.trim() || undefined,
      status: statusFilter.value || undefined,
      task_type: typeFilter.value || undefined,
      limit: 100,
    })
    tasks.value = result.items
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法加载任务')
  } finally {
    loading.value = false
  }
}

function connectSocket(): void {
  if (socket) socket.close()
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  socket = new WebSocket(`${protocol}//${location.host}/api/ws/task-events`)
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data) as { type: string; data?: TaskSnapshot[] }
    if (
      message.type === 'tasks.snapshot' ||
      message.type === 'tasks.delta' ||
      message.type === 'task.delta'
    ) {
      const incoming = message.data ?? []
      if (message.type === 'tasks.snapshot') {
        tasks.value = incoming
      } else if (incoming.length) {
        const byId = new Map(tasks.value.map((item) => [item.id, item]))
        for (const item of incoming) {
          byId.set(item.id, item)
          if (selected.value?.id === item.id) {
            selected.value = item
            if (drawerVisible.value) void refreshSelectedItems()
          }
        }
        tasks.value = [...byId.values()].sort((left, right) =>
          right.created_at.localeCompare(left.created_at),
        )
      }
    }
  }
  socket.onclose = () => {
    if (disposed) return
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(connectSocket, 1500)
  }
}

async function openDetails(task: TaskSnapshot): Promise<void> {
  selected.value = task
  drawerVisible.value = true
  await refreshSelectedItems()
}

async function refreshSelectedItems(): Promise<void> {
  if (!selected.value) return
  try {
    items.value = (await api.listTaskItems(selected.value.id, { limit: 500 })).items
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法加载任务明细')
  }
}

async function pause(task: TaskSnapshot): Promise<void> {
  try {
    const result = await api.pauseTask(task.id)
    tasks.value = tasks.value.map((item) => (item.id === result.id ? result : item))
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法暂停任务')
  }
}

async function resume(task: TaskSnapshot): Promise<void> {
  try {
    const result = await api.resumeTask(task.id)
    tasks.value = tasks.value.map((item) => (item.id === result.id ? result : item))
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法继续任务')
  }
}

async function cancel(task: TaskSnapshot): Promise<void> {
  try {
    const result = await api.cancelTask(task.id)
    tasks.value = tasks.value.map((item) => (item.id === result.id ? result : item))
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法取消任务')
  }
}

async function retry(task: TaskSnapshot): Promise<void> {
  try {
    await api.retryFailedTask(task.id)
    await loadTasks()
    ElMessage.success('失败页面已重新加入队列')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法重试失败页面')
  }
}

function stageSummary(item: TaskItemRead): string {
  return item.stages.map((stage) => `${stage.stage}:${stage.status}`).join(' · ')
}

onMounted(() => {
  disposed = false
  void loadTasks()
  connectSocket()
})
onBeforeUnmount(() => {
  disposed = true
  if (reconnectTimer) clearTimeout(reconnectTimer)
  socket?.close()
})
</script>

<template>
  <section class="page-shell task-center-view">
    <div class="page-heading">
      <div>
        <h1>任务中心</h1>
      </div>
      <el-button :icon="RefreshRight" :loading="loading" @click="loadTasks">刷新</el-button>
    </div>

    <div class="task-filters">
      <el-input
        v-model="projectFilter"
        clearable
        placeholder="项目 ID（可选）"
        @keyup.enter="loadTasks"
      />
      <el-select v-model="typeFilter" clearable placeholder="全部类型" @change="loadTasks">
        <el-option label="项目导入" value="project_import" />
        <el-option label="批处理" value="batch_pipeline" />
        <el-option label="批量检测" value="page_detection" />
        <el-option label="OCR" value="ocr" />
        <el-option label="翻译" value="quick_translation" />
        <el-option label="章节摘要" value="chapter_summary" />
        <el-option label="页面修复" value="page_repair" />
        <el-option label="页面生成" value="page_render" />
        <el-option label="ZIP 导出" value="export_zip" />
        <el-option label="EPUB 导出" value="export_epub" />
      </el-select>
      <el-select v-model="statusFilter" clearable placeholder="全部状态" @change="loadTasks">
        <el-option
          v-for="(label, value) in statusLabels"
          :key="value"
          :label="label"
          :value="value"
        />
      </el-select>
    </div>

    <el-empty v-if="!loading && !filtered.length" description="暂无任务" />
    <RecycleScroller v-else class="task-scroller" :items="filtered" :item-size="118" key-field="id">
      <template #default="{ item }">
        <article class="task-card" :class="`task-${item.status}`">
          <button class="task-card-main" type="button" @click="openDetails(item)">
            <div class="task-card-title">
              <strong>{{ typeLabels[item.task_type] ?? item.task_type }}</strong>
              <el-tag size="small">{{ statusLabels[item.status] ?? item.status }}</el-tag>
            </div>
            <p>
              {{ statusLabels[item.stage] ?? item.stage }} · {{ item.completed }}/{{ item.total }}
              页
              <span v-if="item.queue_position"> · 队列第 {{ item.queue_position }} 位</span>
            </p>
            <el-progress
              :percentage="
                item.total
                  ? Math.round(((item.completed + item.failed + item.skipped) / item.total) * 100)
                  : 0
              "
              :show-text="false"
            />
            <small v-if="item.active_page_id">当前页：{{ item.active_page_id }}</small>
            <small v-if="item.status === 'pausing'" class="task-pausing-note"
              >正在完成当前请求…</small
            >
          </button>
          <div class="task-card-actions">
            <el-button v-if="item.can_pause" text :icon="VideoPause" @click="pause(item)"
              >暂停</el-button
            >
            <el-button v-if="item.can_resume" text :icon="VideoPlay" @click="resume(item)"
              >继续</el-button
            >
            <el-button
              v-if="!['completed', 'failed', 'cancelled'].includes(item.status)"
              text
              :icon="Close"
              @click="cancel(item)"
              >取消</el-button
            >
            <el-button
              v-if="
                item.task_type === 'batch_pipeline' &&
                item.failed &&
                ['completed', 'failed', 'cancelled'].includes(item.status)
              "
              text
              type="warning"
              @click="retry(item)"
              >重试失败</el-button
            >
          </div>
        </article>
      </template>
    </RecycleScroller>

    <el-drawer v-model="drawerVisible" title="任务明细" size="520px">
      <template v-if="selected">
        <p class="dialog-note">
          {{ selected.stage }} · 已完成 {{ selected.completed }}，失败 {{ selected.failed }}，跳过
          {{ selected.skipped }}
        </p>
        <el-table :data="items" size="small" max-height="600">
          <el-table-column prop="page_index" label="页" width="60" />
          <el-table-column prop="status" label="页面状态" width="100" />
          <el-table-column label="阶段">
            <template #default="scope">{{ stageSummary(scope.row) }}</template>
          </el-table-column>
        </el-table>
      </template>
    </el-drawer>
  </section>
</template>
