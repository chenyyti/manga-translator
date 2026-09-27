import { expect, test } from '@playwright/test'

test('settings overview stays usable while a lower section loads', async ({ page }) => {
  let releaseFonts = () => {}
  const fontGate = new Promise<void>((resolve) => {
    releaseFonts = resolve
  })
  await page.route('**/api/fonts', async (route) => {
    await fontGate
    await route.continue()
  })

  try {
    await page.goto('/settings')
    await expect(page.locator('#settings-overview.settings-grid')).toBeVisible()
    await expect(page.locator('#settings-render .el-loading-mask')).toBeVisible()
  } finally {
    releaseFonts()
  }
  await expect(page.locator('#settings-render .el-loading-mask')).toBeHidden()
})

test('returning to project and bookshelf lists keeps their content visible', async ({ page }) => {
  await page.goto('/projects')
  await expect(page.locator('.projects-page .skeleton-list')).toBeHidden()

  let releaseProjects = () => {}
  const projectGate = new Promise<void>((resolve) => {
    releaseProjects = resolve
  })
  await page.route('**/api/projects?*', async (route) => {
    await projectGate
    await route.continue()
  })
  await page.getByRole('button', { name: '任务中心' }).click()
  await page.getByRole('button', { name: '翻译项目', exact: true }).click()
  await expect(page.getByRole('heading', { name: '翻译项目', exact: true })).toBeVisible()
  await expect(page.locator('.projects-page .skeleton-list')).toBeHidden()
  releaseProjects()

  await page.getByRole('button', { name: '我的书架', exact: true }).click()
  await expect(page.getByRole('heading', { name: '我的书架', exact: true })).toBeVisible()
  await expect(page.getByLabel('正在加载书架')).toBeHidden()
  await expect
    .poll(() => page.evaluate(() => performance.getEntriesByName('bookshelf-ready').length))
    .toBeGreaterThan(0)

  let releaseBookshelf = () => {}
  const bookshelfGate = new Promise<void>((resolve) => {
    releaseBookshelf = resolve
  })
  await page.route('**/api/bookshelf?*', async (route) => {
    await bookshelfGate
    await route.continue()
  })
  await page.getByRole('button', { name: '任务中心' }).click()
  await page.getByRole('button', { name: '我的书架', exact: true }).click()
  await expect(page.getByRole('heading', { name: '我的书架', exact: true })).toBeVisible()
  await expect(page.getByLabel('正在加载书架')).toBeHidden()
  releaseBookshelf()
})
