import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'vue-virtual-scroller/dist/vue-virtual-scroller.css'

import App from './App.vue'
import router from './router'
import { registerWebMcpTools } from './webmcp'
import './styles/main.css'

const app = createApp(App)
let canvasRegistered = false
router.beforeEach(async (to) => {
  if (!canvasRegistered && to.name === 'project') {
    app.use((await import('vue-konva')).default)
    canvasRegistered = true
  }
})
app.use(createPinia())
app.use(router)
app.use(ElementPlus)
app.mount('#app')

registerWebMcpTools(router)
