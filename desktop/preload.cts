import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('cmvDesktop', Object.freeze({
    chooseFolder: (options?: { title?: string; defaultPath?: string }) => ipcRenderer.invoke('cmv:choose-folder', options),
    retry: () => ipcRenderer.invoke('cmv:retry'),
    openLogs: () => ipcRenderer.invoke('cmv:open-logs'),
}));
