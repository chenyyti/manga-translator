import { createRouter, createWebHistory } from 'vue-router'

const loadBookshelf = () => import('@/views/BookshelfView.vue')
const loadProjects = () => import('@/views/ProjectsView.vue')
const loadTasks = () => import('@/views/TaskCenterView.vue')
const loadNewProject = () => import('@/views/NewProjectView.vue')
const loadSettings = () => import('@/views/SettingsView.vue')
const loadProjectEditor = () => import('@/views/ProjectView.vue')

export function preloadPrimaryViews(): void {
  void Promise.allSettled([
    loadBookshelf(),
    loadProjects(),
    loadTasks(),
    loadNewProject(),
    loadSettings(),
  ])
}

export function preloadProjectEditor(): void {
  void Promise.allSettled([loadProjectEditor(), import('vue-konva')])
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'bookshelf', component: loadBookshelf },
    { path: '/projects', name: 'projects', component: loadProjects },
    { path: '/tasks', name: 'tasks', component: loadTasks },
    {
      path: '/projects/new',
      name: 'new-project',
      component: loadNewProject,
    },
    {
      path: '/projects/:id/characters',
      name: 'characters',
      redirect: (to) => ({ name: 'project', params: { id: to.params.id }, query: to.query }),
    },
    {
      path: '/projects/:id/quick',
      name: 'project-quick',
      redirect: (to) => ({ name: 'project', params: { id: to.params.id }, query: to.query }),
    },
    {
      path: '/projects/:id/refined',
      name: 'project-refined',
      redirect: (to) => ({ name: 'project', params: { id: to.params.id }, query: to.query }),
    },
    {
      path: '/reader/:projectId',
      name: 'reader',
      component: () => import('@/views/ReaderView.vue'),
    },
    {
      path: '/projects/:id',
      name: 'project',
      component: loadProjectEditor,
    },
    { path: '/settings', name: 'settings', component: loadSettings },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

export default router
