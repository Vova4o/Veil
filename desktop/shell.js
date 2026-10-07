document.querySelector('#retry').addEventListener('click', async event => {
    event.target.disabled = true;
    try { await window.cmvDesktop.retry(); } finally { event.target.disabled = false; }
});
document.querySelector('#logs').addEventListener('click', () => window.cmvDesktop.openLogs());
