import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('mediaStudioDesktop', Object.freeze({
    chooseFolder: (options?: { title?: string; defaultPath?: string }) => ipcRenderer.invoke('mediastudio:choose-folder', options),
    retry: () => ipcRenderer.invoke('mediastudio:retry'),
    openLogs: () => ipcRenderer.invoke('mediastudio:open-logs'),
}));
