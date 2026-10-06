/** Shared presentation for the Viewer's source and album pickers. */
function safeCount(value) {
    const count = Number(value);
    return Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
}

export function collectionCount(collection, { album = false } = {}) {
    const images = safeCount(collection.image_count);
    const videos = safeCount(collection.video_count);
    const count = collection.asset_count == null
        ? images + videos
        : safeCount(collection.asset_count);
    const unit = album || videos > 0 ? 'item' : 'image';
    return {
        count,
        label: `${count.toLocaleString()} ${unit}${count === 1 ? '' : 's'}`,
        description: album ? '' : `${images.toLocaleString()} image${images === 1 ? '' : 's'}, ${videos.toLocaleString()} video${videos === 1 ? '' : 's'}`,
    };
}

export function sourcePresentation(source) {
    const status = source.enabled === false ? 'disabled' : (source.source_status || 'available');
    const labels = {
        disabled: 'Disabled', available: 'Available', partially_available: 'Partial',
        unavailable: 'Unavailable', reconnecting: 'Reconnecting', error: 'Error',
    };
    const processing = source.enabled !== false && source.status === 'processing';
    const total = source.processed_asset_count == null
        ? safeCount(source.image_count)
        : collectionCount(source).count;
    const processed = safeCount(source.processed_asset_count ?? source.processed_count);
    const percent = total > 0 ? Math.min(100, Math.round(processed / total * 100)) : null;
    return { status, label: labels[status] || 'Unknown', processing, percent };
}

export function collectionIcon(name, size = 16) {
    const sprite = document.documentElement?.dataset.viewerIcons || '/static/icons/viewer.svg';
    return `<svg class="viewer-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="${sprite}#${name}"></use></svg>`;
}

export function updateCollectionViewButton(button, mode, collectionName) {
    if (!button) return;
    const listMode = mode === 'list';
    button.innerHTML = collectionIcon(listMode ? 'grid-2x2' : 'list');
    const label = `Show ${collectionName} as ${listMode ? 'tiles' : 'a list'}`;
    button.title = label;
    button.setAttribute('aria-label', label);
    button.classList.remove('active');
}

/** Native popover supplies Escape, outside dismissal and keyboard focus return. */
export function initSourceActions(item) {
    const button = item.querySelector('.source-menu-toggle');
    const menu = item.querySelector('.source-actions');
    menu.addEventListener('toggle', event => {
        const open = event.newState === 'open';
        button.setAttribute('aria-expanded', String(open));
        if (!open) return;
        const anchor = button.getBoundingClientRect();
        const panel = menu.getBoundingClientRect();
        const left = Math.max(12, Math.min(anchor.right - panel.width, window.innerWidth - panel.width - 12));
        const top = anchor.bottom + panel.height + 8 <= window.innerHeight - 12
            ? anchor.bottom + 6 : Math.max(12, anchor.top - panel.height - 6);
        menu.style.left = `${left}px`;
        menu.style.top = `${top}px`;
    });
    menu.addEventListener('click', event => {
        if (event.target.closest('button') && !event.target.closest('button').disabled) menu.hidePopover();
    });
}

export function captureSourceInteraction(list) {
    const openMenu = list.querySelector('.source-actions:popover-open');
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    const menuSource = openMenu?.closest('.source-folder-item');
    const focusedSource = focused?.closest('.source-folder-item');
    if (!menuSource && !focusedSource) return null;
    const selector = ['.source-toggle-btn', '.source-recursive-btn', '.source-reconcile-btn',
        '.folder-delete-btn', '.source-menu-toggle', '.folder-item-content'].find(value => focused?.matches(value));
    return { menuId: menuSource?.dataset.sourceId, focusId: focusedSource?.dataset.sourceId, selector };
}

export function restoreSourceInteraction(list, interaction) {
    if (!interaction) return;
    const menuSource = list.querySelector(`[data-source-id="${interaction.menuId}"]`);
    menuSource?.querySelector('.source-actions').showPopover();
    if (!interaction.selector) return;
    const source = list.querySelector(`[data-source-id="${interaction.focusId}"]`);
    if (!source) return;
    const control = source.querySelector(interaction.selector);
    (control?.disabled ? source.querySelector('.source-menu-toggle') : control)?.focus({ preventScroll: true });
}
