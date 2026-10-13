/** Shared maintenance UI. Closing it leaves the durable server job running. */
let currentRepair = null;
let polling = false;
let submitting = false;

const dialog = document.createElement('dialog');
dialog.className = 'library-repair-dialog';
dialog.setAttribute('aria-labelledby', 'library-repair-title');
dialog.innerHTML = `<div class="library-repair-heading"><h2 id="library-repair-title">Check and repair library</h2><button type="button" class="btn repair-close" aria-label="Close repair status">Close</button></div>
    <p>Check sources, recover missing metadata and fix broken previews. Albums, ratings, notes and custom order are kept.</p>
    <p class="repair-status" role="status" aria-live="polite"></p>
    <progress aria-label="Library check progress"></progress>
    <p class="repair-summary"></p>
    <details hidden><summary>Files that need attention</summary><ul></ul></details>
    <button type="button" class="btn repair-retry" hidden>Try again</button>`;
document.body.appendChild(dialog);
dialog.querySelector('.repair-close').onclick = () => dialog.close();
dialog.querySelector('.repair-retry').onclick = () => startLibraryRepair(currentRepair?.source_id);

const active = repair => repair && ['pending', 'running'].includes(repair.status);

function render(repair) {
    const progress = dialog.querySelector('progress');
    const status = dialog.querySelector('.repair-status');
    const summary = dialog.querySelector('.repair-summary');
    const busy = active(repair);
    document.querySelectorAll('#btn-repair-library').forEach(button => {
        button.setAttribute('aria-busy', String(Boolean(busy)));
        const label = button.querySelector('strong');
        if (label) label.textContent = busy ? 'Repair in progress…' : 'Check and repair library';
    });
    if (!repair) {
        status.textContent = 'Starting library check…';
        progress.removeAttribute('value');
        summary.textContent = '';
        progress.hidden = false;
        dialog.querySelector('details').hidden = true;
        dialog.querySelector('.repair-retry').hidden = true;
        return;
    }
    const phases = { queued: 'Waiting to start…', retrying: 'Waiting to retry the library check…', checking_sources: 'Checking source folders…',
        checking_assets: `Checking files: ${repair.checked} of ${repair.total}`, finished: 'Library check finished.' };
    status.textContent = repair.status === 'failed' ? 'Library check could not finish.' : phases[repair.phase] || 'Checking library…';
    if (repair.total > 0) {
        progress.max = repair.total;
        progress.value = repair.checked;
    } else progress.removeAttribute('value');
    progress.hidden = !busy;
    summary.textContent = `${repair.repaired} repaired · ${repair.unchanged} already healthy · ${repair.skipped} skipped · ${repair.failed || 0} could not be repaired`;
    const details = dialog.querySelector('details');
    details.hidden = !(repair.errors?.length);
    const list = details.querySelector('ul');
    list.replaceChildren(...(repair.errors || []).map(message => {
        const item = document.createElement('li');
        item.textContent = message;
        return item;
    }));
    dialog.querySelector('.repair-retry').hidden = busy || (!repair.errors?.length && repair.status !== 'failed');
}

async function request(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'Library check is unavailable');
    return body.repair;
}

async function poll() {
    if (polling) return;
    polling = true;
    try {
        currentRepair = await request('/api/library/repair');
        render(currentRepair);
        if (!active(currentRepair)) {
            window.dispatchEvent(new CustomEvent('library-repaired'));
            return;
        }
    } catch (error) {
        dialog.querySelector('.repair-status').textContent = `${error.message}. Reconnecting…`;
    } finally {
        polling = false;
    }
    setTimeout(poll, 1000);
}

export async function startLibraryRepair(sourceId = null) {
    if (!dialog.open) dialog.showModal();
    if (submitting || active(currentRepair)) return;
    submitting = true;
    render(null);
    try {
        currentRepair = await request('/api/library/repair', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_id: sourceId || null }),
        });
        render(currentRepair);
        poll();
    } catch (error) {
        dialog.querySelector('.repair-status').textContent = error.message;
        dialog.querySelector('progress').hidden = true;
        dialog.querySelector('.repair-retry').hidden = false;
    } finally {
        submitting = false;
    }
}

document.querySelector('#btn-repair-library')?.addEventListener('click', () => startLibraryRepair());
request('/api/library/repair').then(repair => {
    if (submitting || currentRepair) return;
    currentRepair = repair;
    if (active(repair)) { render(repair); poll(); }
}).catch(() => { /* Maintenance remains available if startup status fails. */ });
