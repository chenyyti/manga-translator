import { expect, test, type APIRequestContext } from '@playwright/test'
import { deflateSync } from 'node:zlib'

function crc32(value: Buffer): number {
  let crc = 0xffffffff
  for (const byte of value) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1))
  }
  return (crc ^ 0xffffffff) >>> 0
}

function pngChunk(type: string, data: Buffer): Buffer {
  const name = Buffer.from(type, 'ascii')
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length)
  const checksum = Buffer.alloc(4)
  checksum.writeUInt32BE(crc32(Buffer.concat([name, data])))
  return Buffer.concat([length, name, data, checksum])
}

function whitePng(width = 64, height = 96): Buffer {
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0)
  header.writeUInt32BE(height, 4)
  header.set([8, 2, 0, 0, 0], 8)
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(width * 3, 255)])
  const raw = Buffer.concat(Array.from({ length: height }, () => row))
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ])
}

async function waitForTask(request: APIRequestContext, taskId: string): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/tasks/${taskId}`)
        return (await response.json()).data.status
      },
      { timeout: 20_000 },
    )
    .toBe('completed')
}

test('opens the real Phase 2 annotation workspace', async ({ page, request }) => {
  test.setTimeout(90_000)
  const sessionResponse = await request.post('/api/import-sessions', {
    data: {
      project_name: `浏览器标注验收-${Date.now()}`,
      source_language: 'ja',
      target_language: 'zh-CN',
      translation_mode: 'quick',
      source_type: 'multiple',
    },
  })
  const session = (await sessionResponse.json()).data
  for (let index = 1; index <= 70; index += 1) {
    const filename = '第' + index + '页.png'
    const uploaded = await request.post('/api/import-sessions/' + session.id + '/files', {
      multipart: {
        relative_path: filename,
        file: { name: filename, mimeType: 'image/png', buffer: whitePng() },
      },
    })
    expect(uploaded.ok()).toBe(true)
  }
  const committedResponse = await request.post(`/api/import-sessions/${session.id}/commit`)
  const committed = (await committedResponse.json()).data
  await waitForTask(request, committed.task_id)

  await page.goto(`/projects/${committed.project_id}`)
  await expect(page.getByRole('heading', { name: /浏览器标注验收/ })).toBeVisible()
  await expect(page.locator('[aria-label="项目 OCR Provider"]')).toContainText('MangaOCR')
  await expect(page.getByRole('button', { name: '选择' })).toBeVisible()
  await expect(page.getByRole('button', { name: '检测本页' })).toBeVisible()
  await expect(page.getByRole('button', { name: '批量检测' })).toBeVisible()
  await expect(page.getByRole('button', { name: '批量处理' })).toBeEnabled()
  await page.getByRole('button', { name: '批量处理' }).click()
  const batchDialog = page.getByRole('dialog', { name: '批量处理' })
  await expect(batchDialog.getByText('OCR', { exact: true })).toBeVisible()
  await expect(batchDialog.getByText('YOLO 检测', { exact: true })).toHaveCount(0)
  await batchDialog.getByRole('button', { name: '取消' }).click()
  await expect(page.getByLabel('YOLO 检测图').locator('.source-preview-image')).toBeVisible()
  await expect(page.getByLabel('YOLO 检测图').locator('.detection-editor')).toHaveCount(0)
  await expect(page.getByLabel('翻译成品图')).toContainText('翻译并生成后显示成品图')
  await expect(page.getByLabel('翻译成品图').locator('.rendered-preview-image')).toHaveCount(0)
  await expect(page.getByText('文本区域 0')).toBeVisible()

  for (const width of [1920, 1366, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 })
    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    )
    expect(hasHorizontalOverflow).toBe(false)
    const toolbarFitsViewport = await page.locator('.detection-toolbar').evaluate((toolbar) =>
      [...toolbar.querySelectorAll('button, .el-select')].every((control) => {
        const bounds = control.getBoundingClientRect()
        return bounds.left >= 0 && bounds.right <= document.documentElement.clientWidth
      }),
    )
    expect(toolbarFitsViewport).toBe(true)
  }

  await page.setViewportSize({ width: 390, height: 900 })
  await page.getByRole('button', { name: '第 1 / 70 页' }).click()
  const thumbnailRows = await page
    .locator('.thumbnail-item')
    .evaluateAll((items) => new Set(items.map((item) => (item as HTMLElement).offsetTop)).size)
  expect(thumbnailRows).toBe(1)
  expect(
    await page.locator('.thumbnail-rail').evaluate((rail) => rail.scrollWidth > rail.clientWidth),
  ).toBe(true)
  await page
    .locator('[data-page-index="70"] .thumbnail-button')
    .evaluate((button) => (button as HTMLButtonElement).click())
  await expect(page.locator('[data-page-index="70"] .thumbnail-button')).toHaveClass(/current/)
  await expect
    .poll(() => page.locator('.thumbnail-rail').evaluate((rail) => rail.scrollLeft))
    .toBeGreaterThan(0)
  await expect
    .poll(() =>
      page.locator('[data-page-index="70"]').evaluate((item) => {
        const itemBounds = item.getBoundingClientRect()
        const railBounds = item.parentElement!.parentElement!.getBoundingClientRect()
        return itemBounds.left >= railBounds.left && itemBounds.right <= railBounds.right
      }),
    )
    .toBe(true)

  for (const width of [1920, 1366]) {
    await page.setViewportSize({ width, height: 900 })
    const columns = await page.evaluate(() =>
      ['[aria-label="YOLO 检测图"]', '[aria-label="翻译成品图"]', '.region-panel'].map((selector) =>
        document.querySelector(selector)!.getBoundingClientRect(),
      ),
    )
    expect(
      Math.max(...columns.map((column) => column.top)) -
        Math.min(...columns.map((column) => column.top)),
    ).toBeLessThanOrEqual(1)
    expect(columns[0]!.left).toBeLessThan(columns[1]!.left)
    expect(columns[1]!.left).toBeLessThan(columns[2]!.left)
  }

  await page.setViewportSize({ width: 1024, height: 900 })
  const tabletColumns = await page.evaluate(() =>
    ['[aria-label="YOLO 检测图"]', '[aria-label="翻译成品图"]', '.region-panel'].map((selector) =>
      document.querySelector(selector)!.getBoundingClientRect(),
    ),
  )
  expect(Math.abs(tabletColumns[0]!.top - tabletColumns[1]!.top)).toBeLessThanOrEqual(1)
  expect(tabletColumns[2]!.top).toBeGreaterThan(tabletColumns[0]!.bottom)

  for (const width of [768]) {
    await page.setViewportSize({ width, height: 900 })
    const verticalTops = await page.evaluate(() =>
      ['[aria-label="YOLO 检测图"]', '[aria-label="翻译成品图"]', '.region-panel'].map(
        (selector) => document.querySelector(selector)!.getBoundingClientRect().top,
      ),
    )
    expect(verticalTops).toEqual([...verticalTops].sort((left, right) => left - right))
    expect(new Set(verticalTops).size).toBe(3)
  }
  await page.setViewportSize({ width: 390, height: 900 })
  await expect(page.getByLabel('YOLO 检测图')).toBeVisible()
  await expect(page.getByLabel('翻译成品图')).toBeHidden()
  await page.getByRole('button', { name: '成品图' }).click()
  await expect(page.getByLabel('翻译成品图')).toBeVisible()
  await expect(page.getByLabel('YOLO 检测图')).toBeHidden()
  await page.getByRole('button', { name: '检测图' }).click()
  await expect(page.getByLabel('YOLO 检测图')).toBeVisible()
  expect((await request.delete(`/api/projects/${committed.project_id}`)).ok()).toBe(true)
})

test('shows fixed OCR routing and only editable runtime settings', async ({ page }) => {
  await page.goto('/settings')
  await expect(page.getByRole('heading', { name: '设置', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '模型与默认参数' })).toBeVisible({
    timeout: 20_000,
  })
  await expect(page.getByRole('heading', { name: '本地 Provider 与固定路由' })).toBeVisible()
  await expect(page.getByText(/推理环境(已就绪|未安装)/)).toBeVisible()
  await expect(page.getByRole('textbox', { name: '日文 OCR' })).toHaveValue('MangaOCR（固定）')
  await expect(page.getByRole('textbox', { name: '日文 OCR' })).toBeDisabled()
  await expect(page.getByRole('textbox', { name: '韩文 OCR' })).toHaveValue('PaddleOCR（固定）')
  await expect(page.getByRole('textbox', { name: '韩文 OCR' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '导入 .pt 模型' })).toBeVisible()
  await expect(page.getByText(/YOLO 模型目录：/)).toBeVisible()
  await expect(
    page.locator('.el-form-item').filter({ hasText: '默认模型' }).locator('.el-select'),
  ).toBeVisible()
})

test('updates the fixed OCR display when the source language changes', async ({ page }) => {
  await page.goto('/projects/new')
  await expect(page.getByRole('heading', { name: '开始一本漫画' })).toBeVisible()
  const ocrProvider = page.locator('.creation-summary').getByText('MangaOCR（日文）')
  await expect(ocrProvider).toBeVisible()

  await page.locator('.el-form-item').filter({ hasText: '源语言' }).locator('.el-select').click()
  await page.getByRole('option', { name: '韩文' }).click()

  await expect(
    page.locator('.creation-summary').getByText('PaddleOCR（韩文 / 英文）'),
  ).toBeVisible()
})
