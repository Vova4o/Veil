import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('mediaStudioDesktop', Object.freeze({
    chooseFolder: (options?: { title?: string; defaultPath?: string }) => ipcRenderer.invoke('mediastudio:choose-folder', options),
    retry: () => ipcRenderer.invoke('mediastudio:retry'),
    openLogs: () => ipcRenderer.invoke('mediastudio:open-logs'),
    onStartupStatus: (listener: (text: string) => void) => {
        ipcRenderer.on('mediastudio:startup-status', (_event, text: string) => listener(String(text)));
    },
}));
