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

function chunk(type: string, data: Buffer): Buffer {
  const name = Buffer.from(type, 'ascii')
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length)
  const checksum = Buffer.alloc(4)
  checksum.writeUInt32BE(crc32(Buffer.concat([name, data])))
  return Buffer.concat([length, name, data, checksum])
}

function image(): Buffer {
  const width = 100
  const height = 120
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0)
  header.writeUInt32BE(height, 4)
  header.set([8, 2, 0, 0, 0], 8)
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(width * 3, 255)])
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(Buffer.concat(Array.from({ length: height }, () => row)))),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

async function waitTask(request: APIRequestContext, id: string): Promise<void> {
  await expect
    .poll(async () => (await (await request.get(`/api/tasks/${id}`)).json()).data.status)
    .toBe('completed')
}

test('review, OCR, manual correction and bbox expiration', async ({ page, request }) => {
  const regionId = `e2e-region-${Date.now()}`
  const session = (
    await (
      await request.post('/api/import-sessions', {
        data: {
          project_name: `OCR 浏览器验收-${Date.now()}`,
          source_language: 'ja',
          target_language: 'zh-CN',
          translation_mode: 'quick',
          source_type: 'single',
          ocr_provider: 'auto',
        },
      })
    ).json()
  ).data
  await request.post(`/api/import-sessions/${session.id}/files`, {
    multipart: {
      relative_path: '原稿.png',
      file: { name: '原稿.png', mimeType: 'image/png', buffer: image() },
    },
  })
  const committed = (await (await request.post(`/api/import-sessions/${session.id}/commit`)).json())
    .data
  await waitTask(request, committed.task_id)
  const pageItem = (await (await request.get(`/api/projects/${committed.project_id}/pages`)).json())
    .data.items[0]
  const saved = await request.put(`/api/pages/${pageItem.id}/regions`, {
    data: {
      expected_revision: 0,
      regions: [
        {
          id: regionId,
          class_id: 0,
          class_name: 'text_region',
          x1: 5,
          y1: 5,
          x2: 80,
          y2: 40,
          source: 'manual',
        },
      ],
    },
  })
  expect(saved.ok()).toBe(true)
  expect((await request.post(`/api/pages/${pageItem.id}/review`)).ok()).toBe(true)

  await page.goto(`/projects/${committed.project_id}`)
  await page.getByRole('button', { name: '当前页 OCR' }).click()
  const completedRegion = page.getByRole('button', { name: /#1 text_region 已识别/ })
  await expect(completedRegion).toBeVisible({ timeout: 15_000 })
  await completedRegion.click()
  const source = page.getByPlaceholder('识别结果或人工校对原文')
  await expect(source).toHaveValue('テスト原文', { timeout: 15_000 })
  await source.fill('人工校对原文')
  await page.getByRole('button', { name: '保存原文' }).click()
  await expect(page.getByText('人工原文已保存')).toBeVisible()

  const document = (await (await request.get(`/api/pages/${pageItem.id}/regions`)).json()).data
  document.regions[0].x1 += 1
  expect(
    (
      await request.put(`/api/pages/${pageItem.id}/regions`, {
        data: { expected_revision: document.revision, regions: document.regions },
      })
    ).ok(),
  ).toBe(true)
  await page.reload()
  await page.getByRole('button', { name: /#1 text_region/ }).click()
  await expect(page.getByText(/OCR 已过期：/)).toBeVisible()
  await expect(source).toHaveValue('人工校对原文')
  await page.keyboard.press('Escape')
  await expect(page.getByText('0 个已选')).toBeVisible()
  await page.keyboard.press('Control+A')
  await expect(page.getByText('1 个已选')).toBeVisible()
  expect((await request.delete(`/api/projects/${committed.project_id}`)).ok()).toBe(true)
})
