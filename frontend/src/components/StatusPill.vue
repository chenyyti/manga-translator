<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

const labels: Record<string, string> = {
  importing: '正在导入',
  ready: '已就绪',
  ready_with_warnings: '有页面异常',
  import_failed: '导入失败',
  deleting: '正在删除',
  pending: '等待中',
  pausing: '正在暂停',
  paused: '已暂停',
  processing: '处理中',
  corrupt: '图片损坏',
  failed: '失败',
  completed: '已完成',
  cancelled: '已取消',
  outdated: '已过期',
  partial: '部分完成',
  completed_with_warnings: '完成（有异常）',
}

const tone = computed(() => {
  if (['ready', 'completed'].includes(props.status)) return 'success'
  if (
    [
      'ready_with_warnings',
      'corrupt',
      'outdated',
      'partial',
      'completed_with_warnings',
      'paused',
    ].includes(props.status)
  )
    return 'warning'
  if (['import_failed', 'failed', 'cancelled'].includes(props.status)) return 'danger'
  return 'working'
})
</script>

<template>
  <span class="status-pill" :class="`status-${tone}`">
    <span class="status-dot" aria-hidden="true" />
    {{ labels[status] ?? status }}
  </span>
</template>
