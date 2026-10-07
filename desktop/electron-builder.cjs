module.exports = {
    appId: 'org.lotargo.comfy-meta-viewer', productName: 'Comfy Meta Viewer',
    directories: { output: 'dist/desktop' },
    files: ['.desktop-build/**', 'desktop/shell.html', 'desktop/shell.js', 'desktop/assets/**', 'package.json'],
    extraResources: [{ from: 'build/desktop-backend/comfy-meta-backend', to: 'backend' }],
    asar: true, npmRebuild: false,
    win: { target: 'nsis', icon: 'desktop/assets/icon.ico', artifactName: 'Comfy-Meta-Viewer-${version}-Setup.${ext}' },
    nsis: { oneClick: false, perMachine: false, allowToChangeInstallationDirectory: true,
        deleteAppDataOnUninstall: false, createDesktopShortcut: true },
    mac: { target: 'dmg', category: 'public.app-category.graphics-design' },
    linux: { target: ['AppImage', 'deb'], category: 'Graphics' },
};
