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
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(Buffer.concat(Array.from({ length: height }, () => row)))),
    pngChunk('IEND', Buffer.alloc(0)),
  ])
}

async function waitForTask(request: APIRequestContext, taskId: string): Promise<void> {
  await expect
    .poll(async () => (await (await request.get(`/api/tasks/${taskId}`)).json()).data.status, {
      timeout: 20_000,
    })
    .toBe('completed')
}

test('review, OCR, quick translation, manual edit and translation expiration', async ({
  page,
  request,
}) => {
  const stamp = Date.now()
  const profile = (
    await (
      await request.post('/api/llm-profiles', {
        data: {
          name: `E2E Profile ${stamp}`,
          provider: 'openai',
          base_url: 'https://example.test/v1',
          model: 'e2e-model',
          api_key: 'e2e-secret-83a2',
        },
      })
    ).json()
  ).data
  const session = (
    await (
      await request.post('/api/import-sessions', {
        data: {
          project_name: `翻译浏览器验收-${stamp}`,
          source_language: 'ja',
          target_language: 'zh-CN',
          translation_mode: 'quick',
          source_type: 'single',
          ocr_provider: 'auto',
          llm_profile_id: profile.id,
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
  await waitForTask(request, committed.task_id)
  const pageItem = (await (await request.get(`/api/projects/${committed.project_id}/pages`)).json())
    .data.items[0]
  const regionId = `e2e-translation-region-${stamp}`
  await request.put(`/api/pages/${pageItem.id}/regions`, {
    data: {
      expected_revision: 0,
      regions: [
        {
          id: regionId,
          class_id: 0,
          class_name: 'dialogue',
          x1: 5,
          y1: 5,
          x2: 80,
          y2: 40,
          source: 'manual',
        },
      ],
    },
  })
  await request.post(`/api/pages/${pageItem.id}/review`)

  await page.goto(`/projects/${committed.project_id}`)
  await expect(page).toHaveURL(new RegExp(`/projects/${committed.project_id}$`))
  await expect(page.getByLabel('YOLO 检测图').locator('.detection-editor')).toBeVisible()
  await expect(page.getByLabel('翻译成品图').locator('.rendered-preview-image')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '视觉分析' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '人物' })).toHaveCount(0)
  await page.getByRole('button', { name: '当前页 OCR' }).click()
  const recognizedRegion = page.getByRole('button', { name: /#1 dialogue 已识别/ })
  await expect(recognizedRegion).toBeVisible({ timeout: 15_000 })
  await recognizedRegion.click()
  await expect(page.getByPlaceholder('识别结果或人工校对原文')).toHaveValue('テスト原文', {
    timeout: 15_000,
  })
  await page
    .getByRole('region', { name: '本页流程' })
    .getByRole('button', { name: '开始翻译' })
    .click()
  const translatedRegion = page.getByRole('button', {
    name: /#1 dialogue 已识别 · 已翻译/,
  })
  await expect(translatedRegion).toBeVisible({ timeout: 15_000 })
  await translatedRegion.click()

  const target = page.getByPlaceholder('API 返回的译文，可直接校对')
  await expect(target).toHaveValue(`测试译文-${regionId}`, { timeout: 15_000 })
  await target.fill('人工改译')
  await page.getByRole('button', { name: '保存译文' }).click()
  await expect(page.getByText('人工译文已保存')).toBeVisible()

  const document = (await (await request.get(`/api/pages/${pageItem.id}/regions`)).json()).data
  await request.put(`/api/regions/${regionId}/ocr-text`, {
    data: { text: '更新后的原文', expected_ocr_revision: document.regions[0].ocr_revision },
  })
  await page.reload()
  await page.getByRole('button', { name: new RegExp(`#1 dialogue`) }).click()
  await expect(page.getByText(/译文已过期：/)).toBeVisible()
  await expect(target).toHaveValue('人工改译')

  expect((await request.delete(`/api/projects/${committed.project_id}`)).ok()).toBe(true)
  expect((await request.delete(`/api/llm-profiles/${profile.id}`)).ok()).toBe(true)
})
