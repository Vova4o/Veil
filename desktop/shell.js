document.querySelector('#retry').addEventListener('click', async event => {
    event.target.disabled = true;
    try { await window.mediaStudioDesktop.retry(); } finally { event.target.disabled = false; }
});
document.querySelector('#logs').addEventListener('click', () => window.mediaStudioDesktop.openLogs());
window.mediaStudioDesktop.onStartupStatus?.(text => {
    const status = document.querySelector('[role="status"]');
    if (status && document.documentElement.dataset.state !== 'failed') status.textContent = text;
});
