// frontend/generate-icons.js
const fs = require('fs');
const path = require('path');
const sharp = require('sharp');
const pngToIco = require('png-to-ico');

const buildDir = path.join(__dirname, 'build');
const assetsDir = path.join(__dirname, 'src', 'assets');

// Ensure destination directories exist
if (!fs.existsSync(buildDir)) fs.mkdirSync(buildDir, { recursive: true });
if (!fs.existsSync(assetsDir)) fs.mkdirSync(assetsDir, { recursive: true });

async function generateAssets() {
    console.log('🚀 Starting MediaVault asset generation pipeline...');

    // 1. Create a modern 1024x1024 master icon canvas if a source logo doesn't exist
    const masterSource = path.join(__dirname, 'master-logo.png');
    if (!fs.existsSync(masterSource)) {
        console.log('⚠️ master-logo.png not found. Creating a premium placeholder canvas...');
        const svgBuffer = Buffer.from(`
      <svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
        <rect width="1024" height="1024" rx="220" fill="#0f172a"/>
        <circle cx="512" cy="512" r="320" fill="url(#grad)" stroke="#38bdf8" stroke-width="24"/>
        <path d="M400 380 H624 V440 H400 Z M400 490 H624 V550 H400 Z M400 600 H560 V660 H400 Z" fill="#f8fafc"/>
        <defs>
          <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" style="stop-color:#1e3a8a;stop-opacity:1" />
            <stop offset="100%" style="stop-color:#0369a1;stop-opacity:1" />
          </linearGradient>
        </defs>
      </svg>
    `);
        await sharp(svgBuffer).png().toFile(masterSource);
    }

    // 2. Generate UI Sidebar logo asset (clean PNG)
    const pngTarget = path.join(assetsDir, 'logo.png');
    await sharp(masterSource).resize(512, 512).toFile(pngTarget);
    console.log(`✅ Web-grade logo cached at: ${pngTarget}`);

    // 3. Generate sizing variants for multi-layered Windows ICO file compilation
    const sizes = [16, 32, 48, 64, 128, 256];
    const tempFiles = [];

    for (const size of sizes) {
        const tempPath = path.join(buildDir, `temp-${size}.png`);
        await sharp(masterSource).resize(size, size).toFile(tempPath);
        tempFiles.push(tempPath);
    }

    // 4. Compile into a standard Windows multi-layered .ico bundle
    const icoTarget = path.join(buildDir, 'icon.ico');
    const buf = await pngToIco(tempFiles);
    fs.writeFileSync(icoTarget, buf);
    console.log(`✅ Multi-layered Windows master icon compiled at: ${icoTarget}`);

    // Clean up temporary sizing files
    tempFiles.forEach(file => fs.unlinkSync(file));
    console.log('✨ Asset pipeline completed successfully.');
}

generateAssets().catch(err => {
    console.error('❌ Pipeline failure:', err);
    process.exit(1);
});