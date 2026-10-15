// Record Electron's compositor without seeking individual CSS/SVG animations.
// Run: node_modules/.bin/electron scripts/record_splash.cjs [output-directory]
// Requires FFmpeg. Uses a disposable profile and never starts the app backend.
const { app, BrowserWindow } = require('electron');
const { readFileSync, writeFileSync, mkdirSync, mkdtempSync, existsSync } = require('node:fs');
const { spawn } = require('node:child_process');
const path = require('node:path');
const os = require('node:os');
const { performance } = require('node:perf_hooks');

const root = path.resolve(__dirname, '..');
const output = path.resolve(process.argv[2] || path.join(root, 'build/splash-recording-clean'));
const scratch = mkdtempSync(path.join(os.tmpdir(), 'mediastudio-splash-'));
const fps = 60, duration = 8.6, width = 1920, height = 1080;
const totalFrames = Math.round(fps * duration);
// Chocolatey's shim cannot reliably forward a raw-video stdin pipe on Windows.
const chocolateyFFmpeg = path.join(process.env.ChocolateyInstall || 'C:/ProgramData/chocolatey', 'lib/ffmpeg/tools/ffmpeg/bin/ffmpeg.exe');
const ffmpegExecutable = process.env.FFMPEG_PATH || (existsSync(chocolateyFFmpeg) ? chocolateyFFmpeg : 'ffmpeg');
app.setPath('userData', path.join(scratch, 'profile'));
app.commandLine.appendSwitch('disable-renderer-backgrounding');
app.commandLine.appendSwitch('disable-background-timer-throttling');

function ffmpeg(args) {
    const child = spawn(ffmpegExecutable, ['-hide_banner', '-loglevel', 'error', '-y', ...args], { windowsHide: true });
    let stderr = '';
    child.stderr.on('data', data => { stderr += data.toString(); });
    child.done = new Promise((resolve, reject) => {
        child.once('error', reject);
        child.once('close', code => code === 0 ? resolve() : reject(new Error(`FFmpeg exited ${code}: ${stderr}`)));
    });
    child.done.catch(() => {});
    return child;
}

let window, encoder;
function fail(error) { console.error(error); encoder?.kill(); window?.destroy(); app.exit(1); }
process.on('uncaughtException', fail);
app.whenReady().then(async () => {
    mkdirSync(output, { recursive: true });
    const master = path.join(output, 'splash-lossless.mkv');
    const mark = readFileSync(path.join(root, 'desktop/assets/logo-mark.svg'), 'utf8').replace(/<\?xml[^>]*\?>/, '');
    // Pause declaratively during warmup, then let every animation, including
    // SVG <use> instances, run naturally together. Never seek animations.
    const html = readFileSync(path.join(root, 'desktop/shell.html'), 'utf8')
        .replace('<!--MARK-->', () => mark).replace('<!--STATE-->', 'starting')
        .replace('<!--STATUS-->', 'Starting your library…').replace('<!--ACTIONS-->', 'hidden')
        .replace('<script src="/_desktop/shell.js" defer></script>', '')
        .replace('</head>', '<style id="capture-warmup">*,*::before,*::after{animation-play-state:paused!important}</style></head>');
    const source = path.join(scratch, 'splash.html');
    writeFileSync(source, html);
    encoder = ffmpeg(['-f', 'rawvideo', '-pixel_format', 'bgra', '-video_size', `${width}x${height}`,
        '-framerate', String(fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264rgb', '-preset', 'ultrafast', '-crf', '0', master]);
    encoder.stdin.on('error', fail);
    window = new BrowserWindow({ width: 1280, height: 720, useContentSize: true, show: false,
        webPreferences: { offscreen: { deviceScaleFactor: 1.5 }, backgroundThrottling: false,
            sandbox: true, contextIsolation: true, nodeIntegration: false } });
    window.setContentSize(1280, 720);
    window.webContents.setFrameRate(fps);
    let resolvePaint;
    const firstPaint = new Promise(resolve => { resolvePaint = resolve; });
    let last, start, recording = false, written = 0, paints = 0, held = 0, skipped = 0;
    const frameTimes = [];
    const submit = bitmap => {
        if (bitmap.length !== width * height * 4) throw new Error('Unexpected compositor frame size');
        if (encoder.stdin.writableLength > 256 * 1024 * 1024) throw new Error('Encoder cannot keep up with real-time capture');
        encoder.stdin.write(bitmap);
        written++;
    };
    window.webContents.on('paint', (_event, _dirty, image) => {
        const bitmap = image.toBitmap();
        resolvePaint();
        if (recording) {
            const ms = performance.now() - start;
            const index = Math.min(totalFrames - 1, Math.round(ms * fps / 1000));
            frameTimes.push(ms);
            paints++;
            while (written < index) { submit(last); held++; }
            if (written === index) submit(bitmap);
            else skipped++;
        }
        last = bitmap;
    });
    await window.loadFile(source);
    await window.webContents.executeJavaScript('document.fonts.ready.then(() => true)');
    await firstPaint;
    // Warm the GPU's SVG/filter pipelines before recording the entrance.
    await window.webContents.executeJavaScript('document.getElementById("capture-warmup").remove(); true');
    await new Promise(resolve => setTimeout(resolve, 4800));
    await window.webContents.executeJavaScript(`(() => {
        const style = document.createElement('style');
        style.id = 'capture-warmup';
        style.textContent = '*,*::before,*::after{animation:none!important}';
        document.head.append(style);
        void getComputedStyle(document.getElementById('silk')).animationName;
        style.textContent = '*,*::before,*::after{animation-play-state:paused!important}';
        void getComputedStyle(document.getElementById('silk')).animationName;
    })()`);
    const resetPaint = new Promise(resolve => window.webContents.once('paint', resolve));
    window.webContents.invalidate();
    await resetPaint;
    await new Promise(resolve => setTimeout(resolve, 250));
    start = performance.now();
    recording = true;
    submit(last);
    await window.webContents.executeJavaScript('document.getElementById("capture-warmup").remove(); true');
    console.log('Recording the Electron compositor…');
    await new Promise(resolve => setTimeout(resolve, duration * 1000));
    recording = false;
    while (written < totalFrames) { submit(last); held++; }
    encoder.stdin.end();
    window.destroy();
    await encoder.done;
    const gaps = frameTimes.slice(1).map((time, i) => time - frameTimes[i]);
    const report = { electron: process.versions.electron, chromium: process.versions.chrome,
        width, height, fps, duration, totalFrames: written, compositorPaints: paints,
        heldFrames: held, skippedPaints: skipped, maxPaintGapMs: Math.max(...gaps), scratch, frameTimesMs: frameTimes };
    writeFileSync(path.join(output, 'capture.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ ...report, frameTimesMs: undefined }));
    const video = path.join(output, 'veil-splash-clean-1080p60.mp4');
    await ffmpeg(['-i', master, '-vf', 'fade=t=out:st=8:d=0.6:color=0x030306,noise=alls=2:allf=t+u,format=yuv420p',
        '-c:v', 'libx264', '-preset', 'slow', '-crf', '12', '-tune', 'animation', '-movflags', '+faststart', video]).done;
    await ffmpeg(['-i', master, '-vf', 'fade=t=out:st=8:d=0.6:color=0x030306,fps=30,crop=1200:1000:360:40,scale=600:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a',
        '-loop', '0', path.join(output, 'veil-splash-clean.gif')]).done;
    console.log(`Saved recording to ${output}`);
    app.quit();
}).catch(fail);
app.on('window-all-closed', () => {});
