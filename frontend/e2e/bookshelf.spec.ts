import { expect, test } from '@playwright/test'

test('opens on the bookshelf with the primary import action', async ({ page }) => {
  const canvasRequests: string[] = []
  page.on('request', (request) => {
    if (/konva/i.test(request.url())) canvasRequests.push(request.url())
  })
  await page.goto('/')
  await expect(page).toHaveTitle('漫画智能翻译')
  await expect(page.getByRole('heading', { name: '我的书架' })).toBeVisible()
  await expect(page.getByRole('button', { name: '新建项目' }).first()).toBeVisible()
  await expect
    .poll(() => page.evaluate(() => performance.getEntriesByName('bookshelf-ready').length))
    .toBe(1)
  expect(canvasRequests).toEqual([])
})
