module.exports = {
    appId: 'org.lotargo.veil', productName: 'VEIL',
    directories: { output: 'dist/desktop' },
    files: ['.desktop-build/**', 'desktop/shell.html', 'desktop/shell.js', 'desktop/assets/**', 'package.json'],
    extraResources: [{ from: 'build/desktop-backend/mediastudio-backend', to: 'backend' }],
    asar: true, npmRebuild: false,
    win: { target: 'nsis', icon: 'desktop/assets/icon.ico', artifactName: 'VEIL-${version}-Setup.${ext}' },
    nsis: { oneClick: false, perMachine: false, allowToChangeInstallationDirectory: true,
        deleteAppDataOnUninstall: false, createDesktopShortcut: true },
    mac: { target: 'dmg', category: 'public.app-category.graphics-design' },
    linux: { target: ['AppImage', 'deb'], category: 'Graphics' },
};
