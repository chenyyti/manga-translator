import { expect, test, type APIRequestContext } from '@playwright/test'

function tinyPng(): Buffer {
  // 1x1 white PNG; importing it is enough to exercise the local render task.
  return Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
    'base64',
  )
}

async function waitTask(request: APIRequestContext, id: string): Promise<void> {
  await expect
    .poll(async () => (await (await request.get(`/api/tasks/${id}`)).json()).data.status)
    .toBe('completed')
}

test('shows detection only after review and final image only after generation', async ({
  page,
  request,
}) => {
  const stamp = Date.now()
  const session = (
    await (
      await request.post('/api/import-sessions', {
        data: {
          project_name: `Phase6-${stamp}`,
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
      relative_path: '第1页.png',
      file: { name: '第1页.png', mimeType: 'image/png', buffer: tinyPng() },
    },
  })
  const committed = (await (await request.post(`/api/import-sessions/${session.id}/commit`)).json())
    .data
  await waitTask(request, committed.task_id)
  const pageItem = (await (await request.get(`/api/projects/${committed.project_id}/pages`)).json())
    .data.items[0]

  await page.goto('/projects/' + committed.project_id)
  await expect(page.getByLabel('YOLO 检测图').locator('.source-preview-image')).toBeVisible()
  await expect(page.getByLabel('YOLO 检测图').locator('.detection-editor')).toHaveCount(0)
  await expect(page.getByLabel('翻译成品图').locator('.rendered-preview-image')).toHaveCount(0)
  await request.put(`/api/pages/${pageItem.id}/regions`, {
    data: { expected_revision: 0, regions: [] },
  })
  await request.post(`/api/pages/${pageItem.id}/review`)

  await page.goto(`/projects/${committed.project_id}`)
  await expect(page.getByLabel('YOLO 检测图').locator('.detection-editor')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始生成' })).toBeEnabled()
  await page.getByRole('button', { name: '开始生成' }).click()
  await expect(page.getByLabel('翻译成品图').locator('.rendered-preview-image')).toBeVisible({
    timeout: 15_000,
  })

  const document = (await (await request.get('/api/pages/' + pageItem.id + '/regions')).json()).data
  await request.put('/api/pages/' + pageItem.id + '/regions', {
    data: {
      expected_revision: document.revision,
      regions: [
        {
          id: 'e2e-stale-region-' + stamp,
          class_id: 0,
          class_name: 'dialogue',
          x1: 0,
          y1: 0,
          x2: 1,
          y2: 1,
          source: 'manual',
        },
      ],
    },
  })
  await page.reload()
  await expect(page.getByLabel('翻译成品图')).toContainText('成品已过期，请重新生成')
  await expect(page.getByLabel('翻译成品图').locator('.rendered-preview-image')).toHaveCount(0)

  expect((await request.delete(`/api/projects/${committed.project_id}`)).ok()).toBe(true)
})
