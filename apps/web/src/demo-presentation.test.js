import test from 'node:test'
import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import {
  HERO_ARTWORK_OPTIONS,
  DEMO_DISCLOSURE_TITLE,
  DEMO_DISCLOSURE_TEXT,
} from './demo-presentation.js'

const assetDirectory = fileURLToPath(new URL('../public/assets/', import.meta.url))
const appSourcePath = fileURLToPath(new URL('./main.jsx', import.meta.url))

test('the four selectable Overview themes are distinct local SVG assets', async () => {
  assert.equal(HERO_ARTWORK_OPTIONS.length, 4)
  assert.equal(new Set(HERO_ARTWORK_OPTIONS.map((item) => item.id)).size, 4)
  assert.equal(new Set(HERO_ARTWORK_OPTIONS.map((item) => item.asset)).size, 4)

  for (const option of HERO_ARTWORK_OPTIONS) {
    assert.match(option.asset, /^\/assets\/[a-z0-9-]+\.svg$/)
    const source = await readFile(path.join(assetDirectory, path.basename(option.asset)), 'utf8')
    assert.match(source, /<svg\b/)
    assert.match(source, /<title>/)
    assert.doesNotMatch(source, /<script\b|<foreignObject\b|<image\b|javascript:/i)
    assert.doesNotMatch(source, /(?:href|src)\s*=\s*["']https?:/i)
  }
})

test('the public artwork directory contains no raster image assets', async () => {
  const files = await readdir(assetDirectory)
  assert.deepEqual(files.filter((file) => !file.toLowerCase().endsWith('.svg')).sort(), [])
  assert.equal(files.length, 4)
})

test('the customer-facing disclosure clearly states demo-only and no-reliance boundaries', () => {
  assert.match(DEMO_DISCLOSURE_TITLE, /DEMO ONLY/i)
  assert.match(DEMO_DISCLOSURE_TITLE, /SYNTHETIC/i)
  assert.match(DEMO_DISCLOSURE_TEXT, /not a bank/i)
  assert.match(DEMO_DISCLOSURE_TEXT, /artificial/i)
  assert.match(DEMO_DISCLOSURE_TEXT, /No live institution/i)
  assert.match(DEMO_DISCLOSURE_TEXT, /not financial, investment, tax or legal advice/i)
  assert.match(DEMO_DISCLOSURE_TEXT, /must not be used to make real financial decisions/i)
  assert.match(DEMO_DISCLOSURE_TEXT, /not guaranteed accurate or suitable for reliance/i)
  assert.match(DEMO_DISCLOSURE_TEXT, /Never enter real personal or financial data/i)
})


test('the synthetic-data warning is mounted in the shared application shell', async () => {
  const appSource = await readFile(appSourcePath, 'utf8')
  assert.match(appSource, /<aside className="demo-disclosure" role="note"/)
  assert.match(appSource, /<strong>\{DEMO_DISCLOSURE_TITLE\}<\/strong>/)
  assert.match(appSource, /<p>\{DEMO_DISCLOSURE_TEXT\}<\/p>/)
})
