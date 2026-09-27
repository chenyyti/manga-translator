<script setup lang="ts">
import { Delete, MoreFilled } from '@element-plus/icons-vue'

import StatusPill from './StatusPill.vue'
import type { ProjectSummary } from '@/types'

const props = defineProps<{ project: ProjectSummary; readerMode?: boolean }>()
const emit = defineEmits<{ open: []; read: []; delete: [] }>()

function handlePrimaryAction(): void {
  if (props.readerMode) emit('read')
  else emit('open')
}

const languageNames = { ja: '日文', ko: '韩文', en: '英文' }

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}
</script>

<template>
  <article
    class="project-card"
    :class="{ 'reader-card': readerMode }"
    tabindex="0"
    @dblclick="handlePrimaryAction"
    @keydown.enter="handlePrimaryAction"
  >
    <button
      class="project-cover"
      type="button"
      :aria-label="`打开 ${project.name}`"
      @click="handlePrimaryAction"
    >
      <img
        v-if="project.cover_thumbnail_url"
        :src="project.cover_thumbnail_url"
        alt=""
        loading="lazy"
      />
      <span v-else class="cover-placeholder">
        <span>{{ project.name.slice(0, 1) }}</span>
        <small>{{ String(project.total_pages).padStart(3, '0') }}</small>
      </span>
    </button>
    <div class="project-card-body">
      <div class="project-card-heading">
        <div class="project-statuses">
          <StatusPill :status="project.status" />
          <StatusPill
            v-if="project.render_status && project.render_status !== 'pending'"
            :status="project.render_status"
          />
        </div>
        <el-dropdown
          trigger="click"
          @command="(command: string) => command === 'delete' && $emit('delete')"
        >
          <button class="plain-icon" type="button" aria-label="项目操作">
            <el-icon><MoreFilled /></el-icon>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="delete" :icon="Delete">删除项目</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
      <button class="project-title" type="button" @click="handlePrimaryAction">
        {{ project.name }}
      </button>
      <div class="project-meta">
        <span>{{ languageNames[project.source_language] }} → 简中</span>
        <span>{{ project.total_pages }} 页</span>
      </div>
      <div v-if="readerMode" class="project-reading-progress">
        {{
          project.last_read_page_index
            ? `最近阅读第 ${project.last_read_page_index} 页`
            : '尚未阅读'
        }}
      </div>
      <div class="project-updated">更新于 {{ formatDate(project.updated_at) }}</div>
      <el-button
        v-if="readerMode"
        class="continue-reading"
        type="primary"
        text
        @click="$emit('read')"
      >
        {{ project.last_read_page_index ? '继续阅读' : '开始阅读' }}
      </el-button>
    </div>
  </article>
</template>
