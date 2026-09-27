import { expect, test, type APIRequestContext } from '@playwright/test'

function tinyPng(): Buffer {
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

test('opens a rendered project from the bookshelf and resumes progress', async ({
  page,
  request,
}) => {
  const stamp = Date.now()
  const session = (
    await (
      await request.post('/api/import-sessions', {
        data: {
          project_name: `Phase8-${stamp}`,
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
  await request.put(`/api/pages/${pageItem.id}/regions`, {
    data: { expected_revision: 0, regions: [] },
  })
  await request.post(`/api/pages/${pageItem.id}/review`)
  const rendered = (await (await request.post(`/api/pages/${pageItem.id}/render`)).json()).data
  await waitTask(request, rendered.task_id)

  await page.goto('/')
  await expect(page.getByText(`Phase8-${stamp}`)).toBeVisible()
  await page.getByRole('button', { name: `打开 Phase8-${stamp}` }).click()
  await expect(page).toHaveURL(new RegExp(`/reader/${committed.project_id}\\?page=1`))
  await expect(page.locator('.reader-page-jump')).toContainText('/ 1')
  await expect(page.locator('.reader-image-wrap img')).toBeVisible()

  await request.put(`/api/projects/${committed.project_id}/reading-progress`, {
    data: { page_id: pageItem.id },
  })
  await page.reload()
  await expect(page.locator('.reader-page-jump')).toContainText('/ 1')
  expect(
    (
      await request.put(`/api/projects/${committed.project_id}/cover`, {
        data: { page_id: pageItem.id },
      })
    ).ok(),
  ).toBe(true)
  expect((await request.delete(`/api/projects/${committed.project_id}`)).ok()).toBe(true)
})
