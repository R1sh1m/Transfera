#!/usr/bin/env node
/**
 * MediaVault v2 -- Automated Icon Generation Pipeline
 *
 * Generates the application icon bundle from a programmatically defined SVG:
 *   - frontend/build/icon.ico      (Windows multi-resolution ICO)
 *   - frontend/src/assets/logo.png  (1024x1024 PNG fallback / thumbnail)
 *
 * Dependencies: sharp, png-to-ico (both in devDependencies)
 * Usage: node generate-icons.js  (or npm run icons)
 */

import { mkdirSync, writeFileSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------
const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

const BUILD_DIR = join(__dirname, 'build')
const ASSETS_DIR = join(__dirname, 'src', 'assets')
const ICO_PATH = join(BUILD_DIR, 'icon.ico')
const LOGO_PATH = join(ASSETS_DIR, 'logo.png')

// ICO sizes (Windows standard multi-resolution)
const ICO_SIZES = [16, 32, 48, 64, 128, 256]
const LOGO_SIZE = 1024

// ---------------------------------------------------------------------------
// Inline SVG logo -- MediaVault "M" shield icon
// ---------------------------------------------------------------------------
function buildLogoSVG(size) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#f59e0b"/>
      <stop offset="100%" stop-color="#d97706"/>
    </linearGradient>
    <linearGradient id="inner" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ffffff" stop-opacity="0.25"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0.15"/>
    </linearGradient>
  </defs>
  <!-- Rounded square background -->
  <rect x="40" y="40" width="944" height="944" rx="180" ry="180" fill="url(#bg)"/>
  <rect x="40" y="40" width="944" height="944" rx="180" ry="180" fill="url(#inner)"/>
  <!-- Shield shape -->
  <path d="M512 180 L740 280 L740 520 Q740 720 512 840 Q284 720 284 520 L284 280 Z"
        fill="none" stroke="#ffffff" stroke-width="36" stroke-linejoin="round"/>
  <!-- Letter M -->
  <path d="M380 640 L380 400 L512 520 L644 400 L644 640"
        fill="none" stroke="#ffffff" stroke-width="48" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`
}

// ---------------------------------------------------------------------------
// Generation pipeline
// ---------------------------------------------------------------------------
async function generate() {
  let sharp, pngToIco
  try {
    sharp = (await import('sharp')).default
    pngToIco = (await import('png-to-ico')).default
  } catch (err) {
    console.error(
      'Missing dependencies. Run: npm install\n',
      err.message
    )
    process.exit(1)
  }

  // Ensure output directories exist
  mkdirSync(BUILD_DIR, { recursive: true })
  mkdirSync(ASSETS_DIR, { recursive: true })

  console.log('Generating MediaVault icon bundle...')

  // 1. Generate the full-resolution logo PNG
  const logoSvg = buildLogoSVG(LOGO_SIZE)
  const logoBuffer = Buffer.from(logoSvg)
  await sharp(logoBuffer)
    .resize(LOGO_SIZE, LOGO_SIZE)
    .png()
    .toFile(LOGO_PATH)
  console.log(`  [OK] logo.png  (${LOGO_SIZE}x${LOGO_SIZE}) -> ${LOGO_PATH}`)

  // 2. Generate each ICO size as individual PNGs, then combine
  const icoPngBuffers = []
  for (const size of ICO_SIZES) {
    const sizeSvg = buildLogoSVG(size)
    const sizeBuffer = Buffer.from(sizeSvg)
    const png = await sharp(sizeBuffer)
      .resize(size, size)
      .png()
      .toBuffer()
    icoPngBuffers.push(png)
    console.log(`  [OK] ICO layer ${size}x${size} (${png.length} bytes)`)
  }

  // 3. Combine into .ico
  const icoBuffer = await pngToIco(icoPngBuffers)
  writeFileSync(ICO_PATH, icoBuffer)
  console.log(`  [OK] icon.ico  (${icoBuffer.length} bytes) -> ${ICO_PATH}`)

  console.log('\nIcon bundle generation complete.')
}

generate().catch((err) => {
  console.error('Icon generation failed:', err)
  process.exit(1)
})
