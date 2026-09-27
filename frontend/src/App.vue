<script setup lang="ts">
import AppShell from '@/components/AppShell.vue'
import { useLauncherHeartbeat } from '@/composables/useLauncherHeartbeat'

useLauncherHeartbeat()
</script>

<template>
  <AppShell />
</template>
