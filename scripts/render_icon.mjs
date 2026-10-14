// Render the desktop app icon (icon.png and a multi-size icon.ico) from desktop/assets/logo.svg.
// Run after the logo artwork changes: `node scripts/render_icon.mjs`.
import { Buffer } from 'node:buffer';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const assets = path.join(root, 'desktop', 'assets');
// The artwork is a 400x400 stage; the icon frames the draped V tighter so it reads at 16-48 px.
const CROP = '30 48 340 340';
const ICO_SIZES = [16, 24, 32, 48, 64, 128, 256];

const svg = readFileSync(path.join(assets, 'logo.svg'), 'utf8')
    .replace(/<\?xml[^>]*\?>/, '')
    .replace(/viewBox="[^"]*"/, `viewBox="${CROP}"`)
    .replace(/\swidth="\d+"\s+height="\d+"/, ' width="100%" height="100%"');

function page(size) {
    return `<!doctype html><html><head><style>
html, body { margin: 0; background: transparent; }
div { width: ${size}px; height: ${size}px; border-radius: ${Math.round(size * 0.22)}px; overflow: hidden; }
svg { display: block; }
</style></head><body><div>${svg}</div></body></html>`;
}

// ICO entries may hold PNG data directly (Windows Vista and later).
function ico(images) {
    const header = Buffer.alloc(6 + 16 * images.length);
    header.writeUInt16LE(0, 0);
    header.writeUInt16LE(1, 2);
    header.writeUInt16LE(images.length, 4);
    let offset = header.length;
    images.forEach(({ size, png }, index) => {
        const entry = 6 + 16 * index;
        header.writeUInt8(size >= 256 ? 0 : size, entry);
        header.writeUInt8(size >= 256 ? 0 : size, entry + 1);
        header.writeUInt16LE(1, entry + 4);
        header.writeUInt16LE(32, entry + 6);
        header.writeUInt32LE(png.length, entry + 8);
        header.writeUInt32LE(offset, entry + 12);
        offset += png.length;
    });
    return Buffer.concat([header, ...images.map(image => image.png)]);
}

// Prefer Playwright's own Chromium, fall back to the system Edge so no browser download is needed.
const browser = await chromium.launch().catch(() => chromium.launch({ channel: 'msedge' }));
try {
    const context = await browser.newContext({ deviceScaleFactor: 1 });
    const tab = await context.newPage();
    async function render(size) {
        await tab.setViewportSize({ width: size, height: size });
        await tab.setContent(page(size));
        return tab.locator('div').screenshot({ omitBackground: true });
    }
    writeFileSync(path.join(assets, 'icon.png'), await render(512));
    const images = [];
    for (const size of ICO_SIZES) images.push({ size, png: await render(size) });
    writeFileSync(path.join(assets, 'icon.ico'), ico(images));
} finally {
    await browser.close();
}
console.log('Wrote desktop/assets/icon.png and icon.ico');
