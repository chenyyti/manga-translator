import { expect, test } from '@playwright/test'
import { fileURLToPath } from 'node:url'

const samplePage = fileURLToPath(new URL('./fixtures/sample-page.png', import.meta.url))

test('creates a project without choosing a translation mode', async ({ page, request }) => {
  await page.goto('/projects/new')
  await expect(page.getByRole('heading', { name: '翻译模式' })).toHaveCount(0)
  await expect(page.getByText('精翻', { exact: true })).toHaveCount(0)
  await page.getByRole('textbox', { name: '项目名称' }).fill(`无需模式-${Date.now()}`)
  await page.locator('input[type="file"]').nth(1).setInputFiles(samplePage)
  await page.getByRole('button', { name: '创建并导入' }).click()
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}(?:\?|$)/)

  const projectId = new URL(page.url()).pathname.split('/').pop()
  const project = await request.get(`/api/projects/${projectId}`)
  expect(project.ok()).toBeTruthy()
  expect((await project.json()).data.translation_mode).toBe('quick')

  for (const oldPath of ['quick', 'refined', 'characters']) {
    await page.goto(`/projects/${projectId}/${oldPath}`)
    await expect(page).toHaveURL(new RegExp(`/projects/${projectId}$`))
  }
})
