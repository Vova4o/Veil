import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('cmvDesktop', Object.freeze({
    chooseFolder: () => ipcRenderer.invoke('cmv:choose-folder'),
    retry: () => ipcRenderer.invoke('cmv:retry'),
    openLogs: () => ipcRenderer.invoke('cmv:open-logs'),
}));
