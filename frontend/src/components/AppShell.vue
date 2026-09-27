<script setup lang="ts">
import { Collection, Plus, Setting } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { preloadPrimaryViews } from '@/router'

const route = useRoute()
const router = useRouter()
let preloadTimer: ReturnType<typeof setTimeout> | undefined
onMounted(() => {
  preloadTimer = setTimeout(preloadPrimaryViews, 1200)
})
onBeforeUnmount(() => {
  if (preloadTimer) clearTimeout(preloadTimer)
})
const active = computed(() => {
  if (route.path.startsWith('/tasks')) return 'tasks'
  if (route.path.startsWith('/projects')) return 'projects'
  if (route.path.startsWith('/settings')) return 'settings'
  return 'bookshelf'
})
</script>

<template>
  <div class="app-frame">
    <header class="topbar">
      <button class="brand" type="button" aria-label="返回我的书架" @click="router.push('/')">
        <span class="brand-mark">漫</span>
        <span class="brand-name">漫画智能翻译</span>
      </button>

      <nav class="main-nav" aria-label="主要导航">
        <button
          type="button"
          class="nav-item"
          :class="{ active: active === 'bookshelf' }"
          @click="router.push('/')"
        >
          <el-icon><Collection /></el-icon>
          我的书架
        </button>
        <button
          type="button"
          class="nav-item"
          :class="{ active: active === 'projects' }"
          @click="router.push('/projects')"
        >
          翻译项目
        </button>
        <button
          type="button"
          class="nav-item"
          :class="{ active: active === 'tasks' }"
          @click="router.push('/tasks')"
        >
          任务中心
        </button>
      </nav>

      <div class="topbar-actions">
        <el-button
          class="start-button"
          type="primary"
          :icon="Plus"
          @click="router.push('/projects/new')"
        >
          新建项目
        </el-button>
        <el-tooltip content="设置" placement="bottom">
          <el-button
            class="icon-button"
            circle
            :icon="Setting"
            aria-label="设置"
            @click="router.push('/settings')"
          />
        </el-tooltip>
      </div>
    </header>

    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>
