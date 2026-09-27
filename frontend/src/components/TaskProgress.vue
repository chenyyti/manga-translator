<script setup lang="ts">
import { computed } from 'vue'

import StatusPill from './StatusPill.vue'
import type { TaskSnapshot } from '@/types'

const props = defineProps<{ task: TaskSnapshot }>()
defineEmits<{ cancel: [] }>()

const stageLabels: Record<string, string> = {
  queued: '准备导入',
  preparing: '准备批处理',
  pipeline: '多页并行流水线',
  ocr: '批量 OCR',
  translation: '批量翻译',
  repair: '批量背景修复',
  render: '批量成品生成',
  recovering: '恢复任务',
  validating: '检查文件',
  extracting: '安全解压',
  indexing: '整理页序',
  generating_derivatives: '生成预览',
  loading_model: '加载检测模型',
  loading_ocr_model: '加载 OCR 模型',
  preparing_context: '准备翻译上下文',
  detecting: '检测文本区域',
  recognizing: '识别原文',
  translating: '翻译当前页',
  generating_mask: '生成文字掩膜',
  inpainting_fast: 'FAST 背景修复',
  inpainting_opencv: 'OpenCV 背景修复',
  loading_lama: '加载 Big-LaMa',
  inpainting_lama: 'Big-LaMa 背景修复',
  typesetting: '中文自动排版',
  saving_render: '保存全分辨率成品',
  copying_pages: '复制成品页',
  packaging: '打包导出文件',
  saving_export: '保存导出文件',
  completed_with_warnings: '检测完成（有异常）',
  completed: '导入完成',
  failed: '导入失败',
  cancelled: '已取消',
  paused: '已暂停',
  pausing: '正在完成当前请求',
}
const done = computed(() => props.task.completed + props.task.failed + props.task.skipped)
const percentage = computed(() =>
  props.task.total ? Math.round((done.value / props.task.total) * 100) : 0,
)
const active = computed(() =>
  ['pending', 'running', 'pausing', 'paused'].includes(props.task.status),
)
const taskLabel = computed(() =>
  props.task.task_type === 'page_detection'
    ? '检测任务'
    : props.task.task_type === 'ocr'
      ? 'OCR 任务'
      : props.task.task_type === 'quick_translation'
        ? '翻译任务'
        : props.task.task_type === 'page_repair'
          ? '修复预览任务'
          : props.task.task_type === 'page_render'
            ? '成品生成任务'
            : props.task.task_type === 'batch_pipeline'
              ? '批处理任务'
              : ['export_zip', 'export_epub'].includes(props.task.task_type)
                ? '导出任务'
                : '导入任务',
)
const stageLabel = computed(() => {
  if (props.task.stage === 'completed') {
    if (props.task.task_type === 'page_repair') return '修复完成'
    if (props.task.task_type === 'page_render') return '成品已生成'
  }
  if (props.task.stage === 'completed_with_warnings') {
    if (props.task.task_type === 'page_repair') return '修复完成（有警告）'
    if (props.task.task_type === 'page_render') return '成品生成完成（有警告）'
    if (props.task.task_type === 'batch_pipeline') return '批处理完成（有异常）'
  }
  if (props.task.stage === 'completed' && props.task.task_type === 'batch_pipeline') {
    return '批处理完成'
  }
  if (props.task.stage === 'failed' && props.task.task_type === 'page_render') return '成品生成失败'
  if (props.task.stage === 'failed' && props.task.task_type === 'page_repair') return '修复失败'
  return stageLabels[props.task.stage] ?? props.task.stage
})
</script>

<template>
  <section class="task-progress" aria-live="polite">
    <div class="task-progress-head">
      <div>
        <h2>{{ stageLabel }}</h2>
        <span class="task-kind">{{ taskLabel }}</span>
      </div>
      <StatusPill :status="task.status" />
    </div>
    <el-progress :percentage="percentage" :stroke-width="10" :show-text="false" />
    <div class="task-stats">
      <strong>{{ done }} / {{ task.total || '—' }}</strong>
      <span>成功 {{ task.completed }}</span>
      <span :class="{ 'danger-text': task.failed }">异常 {{ task.failed }}</span>
      <span v-if="task.skipped">跳过 {{ task.skipped }}</span>
    </div>
    <div v-if="task.error_message" class="inline-error">{{ task.error_message }}</div>
    <el-button v-if="active" plain :disabled="task.cancel_requested" @click="$emit('cancel')">
      {{ task.cancel_requested ? '正在取消…' : `取消${taskLabel}` }}
    </el-button>
  </section>
</template>
