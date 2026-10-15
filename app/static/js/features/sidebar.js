/**
 * Viewer navigation: quick filters, source folders and albums.
 * Media cards are rendered only in the central workspace.
 */

import {
    images,
    galleryActive,
    currentFolderId,
    totalImages,
    dom,
    showToast,
    setActiveIndex,
    folders,
    albums,
    currentCollection,
    setFolders,
    setAlbums,
    setImages,
    setTotalImages,
    setCurrentFolderId,
    setCurrentPage,
    setAllLoaded,
    foldersSortKey,
    foldersSortDir,
    foldersViewMode,
    albumsSortKey,
    albumsSortDir,
    albumsViewMode,
    saveState,
    setSidebarCollapsed,
    setSidebarWidth,
    sidebarCollapsed,
    sidebarWidth,
    refreshCacheBuster,
    ratingFilter,
    mediaTypeFilter,
    setRatingFilter,
    setMediaTypeFilter,
    isBrowsableCollection,
} from '../state.js';
import { escapeHtml, customConfirm, formatMediaCountLabel } from '../utils.js';
import { collectionCount, sourcePresentation, collectionIcon, updateCollectionViewButton, initSourceActions, captureSourceInteraction, restoreSourceInteraction } from '../components/viewer-collections.js';

export function updateSidebarImageCount() {
    const loadedCount = images.length;
    const totalCount = totalImages;
    const label = formatMediaCountLabel(loadedCount, totalCount);
    if (dom.imageCount) dom.imageCount.textContent = label;
    if (dom.viewerCollectionCount) {
        const count = totalCount || loadedCount;
        dom.viewerCollectionCount.textContent = count.toLocaleString();
        dom.viewerCollectionCount.title = label;
    }
}

export function renderSidebar() {
    updateSidebarImageCount();
    syncQuickViewsUI();
    syncCollectionPickers();
    import('../components/search-bar.js').then(module => module.applySearchFilter());
}

export function syncCollectionPickers() {
    document.querySelectorAll('.source-folder-item, .album-item').forEach(item => {
        const type = item.dataset.albumId ? 'album' : 'folder';
        const id = Number(item.dataset.albumId || item.dataset.sourceId);
        const active = currentCollection.type === type && currentCollection.id === id;
        item.classList.toggle('active', active);
        const control = type === 'album' ? item : item.querySelector('.folder-item-content');
        if (active) control.setAttribute('aria-current', 'true');
        else control.removeAttribute('aria-current');
    });
}

// Central-image selection helper retained for list/detail interactions.
export async function selectImage(index) {
    const img = images[index];
    if (!img) {
        const { renderImageMeta } = await import('../detail-loader.js');
        await renderImageMeta(null);
        return;
    }
    setActiveIndex(index);
    if (galleryActive) {
        const { openLightbox } = await import('../lightbox.js');
        openLightbox(index, images);
        return;
    }
    const { renderImageMeta } = await import('../detail-loader.js');
    await renderImageMeta(img);
}

export function initSidebarResize() {
    if (!dom.sidebar || !dom.sidebarResize) return;
    let startX;
    let startWidth;

    dom.sidebarResize.addEventListener('mousedown', event => {
        event.preventDefault();
        startX = event.clientX;
        startWidth = dom.sidebar.offsetWidth;
        dom.sidebarResize.classList.add('active');
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
    });

    function onMouseMove(event) {
        const diff = event.clientX - startX;
        dom.sidebar.style.width = Math.min(Math.max(startWidth + diff, 280), 500) + 'px';
    }

    function onMouseUp() {
        setSidebarWidth(dom.sidebar.offsetWidth);
        saveState();
        dom.sidebarResize.classList.remove('active');
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    }

    window.matchMedia('(max-width: 768px)').addEventListener?.('change', applySidebarLayout);
}

export function applySidebarLayout() {
    if (!dom.sidebar) return;
    dom.sidebar.style.width = `${sidebarWidth}px`;
    dom.sidebar.classList.remove('open');
    if (window.matchMedia('(max-width: 768px)').matches) {
        dom.sidebar.classList.remove('collapsed');
        return;
    }
    dom.sidebar.classList.toggle('collapsed', sidebarCollapsed);
}

export function toggleSidebar() {
    if (!dom.sidebar) return;
    if (window.matchMedia('(max-width: 768px)').matches) {
        dom.sidebar.classList.toggle('open');
        dom.sidebar.classList.remove('collapsed');
    } else {
        setSidebarCollapsed(!sidebarCollapsed);
        applySidebarLayout();
        saveState();
    }
}

export async function renderAlbumsList(albumList = null) {
    if (!dom.albumList) return;

    let visibleAlbums = Array.isArray(albumList) ? albumList : albums;
    if (!Array.isArray(albumList) && visibleAlbums.length === 0) {
        const { getAlbums } = await import('../api.js');
        visibleAlbums = await getAlbums();
        setAlbums(visibleAlbums);
    } else if (Array.isArray(albumList)) {
        setAlbums(albumList);
    }

    dom.albumList.classList.toggle('view-list', albumsViewMode === 'list');
    if (dom.albumsCount) dom.albumsCount.textContent = `(${visibleAlbums.length})`;
    updateCollectionViewButton(dom.albumsViewBtn, albumsViewMode, 'albums');

    if (visibleAlbums.length === 0) {
        dom.albumList.innerHTML = `
            <div class="collection-empty viewer-albums-empty">
                ${collectionIcon('images', 24)}
                <strong>No albums yet</strong>
                <p>Group media into albums in Library.</p>
                <a class="btn btn-sm btn-secondary" href="/library">Open Library</a>
            </div>`;
        return;
    }

    const sortedAlbums = [...visibleAlbums].sort((a, b) => {
        const valueA = a[albumsSortKey] ?? '';
        const valueB = b[albumsSortKey] ?? '';
        if (typeof valueA === 'string' && typeof valueB === 'string') {
            const compared = valueA.localeCompare(valueB, undefined, { sensitivity: 'base' });
            return albumsSortDir === 'asc' ? compared : -compared;
        }
        const compared = Number(valueA) - Number(valueB);
        return albumsSortDir === 'asc' ? compared : -compared;
    });

    dom.albumList.innerHTML = '';
    sortedAlbums.forEach(album => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = [
            'folder-item',
            'album-item',
            !album.display_cover_image_id ? 'empty-album' : '',
            currentCollection.type === 'album' && currentCollection.id === album.id ? 'active' : '',
        ].filter(Boolean).join(' ');
        item.dataset.albumDropTarget = String(album.id);
        item.dataset.albumId = String(album.id);
        const { count, label: countLabel } = collectionCount(album, { album: true });
        const active = currentCollection.type === 'album' && currentCollection.id === album.id;
        item.setAttribute('aria-label', `Open album ${album.name}, ${countLabel}`);
        if (active) item.setAttribute('aria-current', 'true');
        const cover = album.display_cover_image_id
            ? `<img src="/api/thumbnail/${album.display_cover_image_id}" alt="" loading="lazy" draggable="false">`
            : `<span class="viewer-album-placeholder" aria-hidden="true">${collectionIcon('images', 24)}</span>`;
        item.innerHTML = `
            <span class="folder-item-content">
                <span class="viewer-album-stack" aria-hidden="true">
                    <span class="viewer-album-cover">${cover}</span>
                </span>
                <span class="folder-item-details">
                    <span class="folder-item-name" title="${escapeHtml(album.name)}">${escapeHtml(album.name)}</span>
                    <span class="viewer-album-meta" title="${countLabel}">${count === 0 ? 'Empty album' : countLabel}</span>
                </span>
            </span>`;
        item.addEventListener('click', async () => {
            const { loadAlbumImages } = await import('../api.js');
            await loadAlbumImages(album.id, album.name);
            await renderAlbumsList();
        });
        dom.albumList.appendChild(item);
    });
}

export async function renderFoldersList(folderList = null) {
    if (!dom.folderList) return;

    let visibleFolders = Array.isArray(folderList) ? folderList : folders;
    if (!Array.isArray(folderList) && visibleFolders.length === 0) {
        const { getFolders } = await import('../api.js');
        visibleFolders = await getFolders();
        setFolders(visibleFolders);
    } else if (Array.isArray(folderList)) {
        setFolders(folderList);
    }

    dom.folderList.classList.toggle('view-list', foldersViewMode === 'list');
    updateCollectionViewButton(dom.foldersViewBtn, foldersViewMode, 'sources');

    if (dom.foldersCount) dom.foldersCount.textContent = `(${visibleFolders.length})`;

    if (visibleFolders.length === 0) {
        dom.folderList.innerHTML = `
            <div class="collection-empty">
                ${collectionIcon('folder-open', 24)}
                <strong>No sources yet</strong>
                <p>Connect a folder to browse its media.</p>
                <button class="btn btn-sm btn-secondary" type="button" data-connect-source>Connect folder</button>
            </div>`;
        dom.folderList.querySelector('[data-connect-source]').onclick = () => dom.btnOpenFolder?.click();
        return;
    }

    // Sort visibleFolders
    const sortedFolders = [...visibleFolders].sort((a, b) => {
        let valA = a[foldersSortKey];
        let valB = b[foldersSortKey];

        if (valA === undefined || valA === null) valA = '';
        if (valB === undefined || valB === null) valB = '';

        if (typeof valA === 'string' && typeof valB === 'string') {
            return foldersSortDir === 'asc' 
                ? valA.localeCompare(valB) 
                : valB.localeCompare(valA);
        }

        // For numbers like image_count
        if (valA < valB) return foldersSortDir === 'asc' ? -1 : 1;
        if (valA > valB) return foldersSortDir === 'asc' ? 1 : -1;
        return 0;
    });

    const interaction = captureSourceInteraction(dom.folderList);
    dom.folderList.innerHTML = '';
    sortedFolders.forEach(folder => {
        const item = document.createElement('div');
        const isSource = !String(folder.path || '').startsWith('__uploads');
        const { status: sourceStatus, label: statusLabel, processing, percent } = sourcePresentation(folder);
        // A healthy source needs no badge; only problems earn a note under the name.
        const showStatus = isSource && !processing && sourceStatus !== 'available';
        const active = currentCollection.type === 'folder' && folder.id === currentFolderId;
        item.className = [
            'folder-item',
            'source-folder-item',
            active ? 'active' : '',
            isSource ? 'physical-source' : '',
            !isSource ? 'virtual-source' : '',
            !folder.enabled ? 'source-disabled' : '',
            folder.enabled && folder.status === 'processing' ? 'source-processing' : '',
            `source-${sourceStatus}`,
        ].filter(Boolean).join(' ');

        item.dataset.sourceId = String(folder.id);
        const { label: countLabel, description: countDescription } = collectionCount(folder);
        const progressHtml = processing ? `
            <span class="folder-progress-wrapper">
                <span class="folder-progress-label">Indexing${percent === null ? '' : ` ${percent}%`}</span>
                <span class="folder-progress-track" role="progressbar" aria-label="Indexing ${escapeHtml(folder.name)}"
                    aria-valuemin="0" aria-valuemax="100" ${percent === null ? '' : `aria-valuenow="${percent}"`}>
                    <span style="width: ${percent ?? 0}%"></span>
                </span>
            </span>` : '';
        const menuId = `viewer-source-actions-${folder.id}`;
        const errorTitle = ` title="${escapeHtml(folder.last_error || statusLabel)}"`;
        const sourceActions = isSource ? `
            <button class="source-action-btn source-toggle-btn" type="button">
                ${collectionIcon('power')}${folder.enabled ? 'Disable source' : 'Enable source'}
            </button>
            <button class="source-action-btn source-recursive-btn" type="button">
                ${collectionIcon('git-branch')}${folder.recursive ? 'Exclude subfolders' : 'Include subfolders'}
            </button>
            <button class="source-action-btn source-reconcile-btn" type="button" ${folder.enabled ? '' : 'disabled'}>
                ${collectionIcon('refresh-cw')}Reconcile now
            </button>` : '';
        item.innerHTML = `
            <button class="folder-item-content" type="button" aria-label="Open source ${escapeHtml(folder.name)}, ${countLabel}"
                ${active ? 'aria-current="true"' : ''}>
                <span class="folder-item-icon" aria-hidden="true">${collectionIcon(active ? 'folder-open' : 'folder', 16)}</span>
                <span class="folder-item-details">
                    <span class="folder-item-name" title="${escapeHtml(folder.path || folder.name)}">${escapeHtml(folder.name)}</span>
                    ${showStatus ? `<span class="source-status source-status-${sourceStatus}"${errorTitle}>${statusLabel}</span>` : ''}
                    ${progressHtml}
                </span>
                <span class="folder-item-meta" title="${countDescription}"><span class="folder-item-count">${countLabel}</span></span>
            </button>
            <button class="source-menu-toggle" type="button" popovertarget="${menuId}"
                aria-controls="${menuId}" aria-expanded="false" aria-label="Actions for ${escapeHtml(folder.name)}" title="Source actions">
                ${collectionIcon('ellipsis')}
            </button>
            <div class="source-actions" id="${menuId}" popover="auto" aria-label="Actions for ${escapeHtml(folder.name)}">
                <div class="source-menu-heading">${escapeHtml(folder.path || folder.name)}</div>
                ${sourceActions}
                <button class="source-action-btn folder-delete-btn" type="button" data-id="${folder.id}">
                    ${collectionIcon('trash')}Forget source
                </button>
            </div>`;
        initSourceActions(item);

        item.querySelector('.folder-item-content').onclick = async () => {
            if (!folder.enabled) {
                const { showToast } = await import('../state.js');
                showToast('Enable the source to view its media');
                return;
            }
            const { loadFolderImages } = await import('../api.js');
            await loadFolderImages(folder.id, folder.name);
            await renderFoldersList(visibleFolders);
        };

        item.querySelector('.source-toggle-btn')?.addEventListener('click', async event => {
            event.stopPropagation();
            const { updateSourceOnServer } = await import('../api.js');
            await updateSourceOnServer(folder.id, { enabled: !folder.enabled });
            await refreshAfterSourceMutation(folder.id);
            const { showToast } = await import('../state.js');
            showToast(folder.enabled ? 'Source disabled' : 'Source enabled; reconciling changes');
        });

        item.querySelector('.source-recursive-btn')?.addEventListener('click', async event => {
            event.stopPropagation();
            const { updateSourceOnServer } = await import('../api.js');
            await updateSourceOnServer(folder.id, { recursive: !folder.recursive });
            await refreshAfterSourceMutation(folder.id);
            const { showToast } = await import('../state.js');
            showToast(folder.recursive ? 'Subfolder scanning disabled' : 'Subfolder scanning enabled');
        });

        item.querySelector('.source-reconcile-btn')?.addEventListener('click', async event => {
            event.stopPropagation();
            const { reconcileSource } = await import('../api.js');
            await reconcileSource(folder.id);
            const { showToast } = await import('../state.js');
            showToast('Source reconciliation queued');
        });

        const deleteBtn = item.querySelector('.folder-delete-btn');
        if (deleteBtn) {
            deleteBtn.onclick = async event => {
                event.stopPropagation();
                const confirmed = await customConfirm('Forget source', `Are you sure you want to remove folder "${folder.name}" from the database? This does not delete any files from your disk.`);
                if (!confirmed) return;

                const { deleteFolderFromServer, getFolders, loadFolderImages, loadSidebarImages } = await import('../api.js');
                if (!await deleteFolderFromServer(folder.id)) return;

                const updatedFolders = await getFolders({ force: true });
                setFolders(updatedFolders);

                if (currentFolderId === folder.id) {
                    const nextFolder = updatedFolders.find(candidate => candidate.enabled);
                    if (nextFolder) {
                        await loadFolderImages(nextFolder.id, nextFolder.name, { force: true });
                    } else {
                        setImages([]);
                        setTotalImages(0);
                        setCurrentFolderId(null);
                        setCurrentPage(0);
                        setAllLoaded(true);
                        setActiveIndex(-1);
                        dom.folderNameEl.textContent = '';
                        saveState();
                        if (galleryActive) {
                            const { renderGallery } = await import('../gallery.js');
                            renderGallery();
                        } else {
                            const { renderImageMeta } = await import('../detail-loader.js');
                            await renderImageMeta(null);
                        }
                    }
                }

                // Refresh the global media cache
                await loadSidebarImages({ force: true });

                await renderFoldersList(updatedFolders);
                const { showToast } = await import('../state.js');
                showToast('Source removed from index');
            };
        }

        dom.folderList.appendChild(item);
    });

    restoreSourceInteraction(dom.folderList, interaction);
    initFoldersSSE();
}

async function refreshAfterSourceMutation(folderId) {
    const { getFolders, loadFolderImages, loadSidebarImages } = await import('../api.js');
    const updatedFolders = await getFolders({ force: true });
    setFolders(updatedFolders);
    const changedFolder = updatedFolders.find(folder => folder.id === folderId);

    if (currentFolderId === folderId) {
        if (changedFolder?.enabled) {
            await loadFolderImages(folderId, changedFolder.name, { force: true });
        } else {
            const nextFolder = updatedFolders.find(folder => folder.enabled);
            if (nextFolder) {
                await loadFolderImages(nextFolder.id, nextFolder.name, { force: true });
            } else {
                setImages([]);
                setTotalImages(0);
                setCurrentFolderId(null);
                setCurrentPage(0);
                setAllLoaded(true);
                setActiveIndex(-1);
                dom.folderNameEl.textContent = '';
                saveState();
                const { renderCurrentContent } = await import('../events.js');
                await renderCurrentContent();
            }
        }
    }

    await loadSidebarImages({ force: true });
    await renderFoldersList(updatedFolders);
}

let foldersEventSource = null;

function initFoldersSSE() {
    if (foldersEventSource) return;

    foldersEventSource = new EventSource('/api/folders/events');
    // Coalesce queued snapshots while applying one update at a time.
    let queuedEvent = null;
    let applyingEvent = false;
    const applyEvent = async (event) => {
        try {
            const state = JSON.parse(event.data);

            // Source rows can appear or disappear after add/remove/reset operations.
            const serverIds = new Set(Object.keys(state));
            const localIds = new Set(folders.map(f => String(f.id)));
            const idsChanged = serverIds.size !== localIds.size || [...serverIds].some(id => !localIds.has(id));

            if (idsChanged) {
                const { getFolders } = await import('../api.js');
                const freshFolders = await getFolders({ force: true });
                setFolders(freshFolders);
                await renderFoldersList(freshFolders);
                return;
            }
            
            let changed = false;
            let sourceRevisionChanged = false;
            const contentChangedSourceIds = new Set();
            let activeSourceDisabled = false;
            const trackedFields = [
                'status',
                'processed_count',
                'processed_asset_count',
                'image_count',
                'video_count',
                'asset_count',
                'enabled',
                'recursive',
                'source_status',
                'last_error',
                'revision',
                'name',
            ];
            const updatedFolders = folders.map(f => {
                const update = state[String(f.id)];
                if (update) {
                    if (trackedFields.some(field => f[field] !== update[field])) {
                        changed = true;
                        sourceRevisionChanged ||= f.revision !== update.revision;
                        if (
                            f.revision !== update.revision
                            || f.processed_count !== update.processed_count
                            || f.processed_asset_count !== update.processed_asset_count
                            || f.image_count !== update.image_count
                            || f.video_count !== update.video_count
                            || f.asset_count !== update.asset_count
                        ) {
                            contentChangedSourceIds.add(f.id);
                        }
                        activeSourceDisabled ||= f.id === currentFolderId && !update.enabled;
                        return { ...f, ...update };
                    }
                }
                return f;
            });

            if (changed) {
                setFolders(updatedFolders);
                await renderFoldersList(updatedFolders);
            }
            if (activeSourceDisabled) {
                await refreshAfterSourceMutation(currentFolderId);
                return;
            }
            if (contentChangedSourceIds.size > 0) {
                // Metadata progress changes details, not the original image bytes.
                if (sourceRevisionChanged) refreshCacheBuster();
                const {
                    invalidateApiCache,
                    loadCollectionImages,
                    loadFolderImages,
                    loadSidebarImages,
                } = await import('../api.js');
                invalidateApiCache();
                await loadSidebarImages({ force: true, preserveCount: true });
                if (currentCollection.type === 'media') {
                    await loadCollectionImages(
                        { ...currentCollection },
                        { force: true, preserveCount: true },
                    );
                }
                const activeFolder = updatedFolders.find(folder => folder.id === currentFolderId && folder.enabled);
                if (activeFolder && contentChangedSourceIds.has(activeFolder.id)) {
                    await loadFolderImages(activeFolder.id, activeFolder.name, { force: true, preserveCount: true });
                }
            }
        } catch (e) {
            console.error('SSE message parsing error:', e);
        }
    };
    foldersEventSource.onmessage = async event => {
        queuedEvent = event;
        if (applyingEvent) return;
        applyingEvent = true;
        try {
            while (queuedEvent) {
                const next = queuedEvent;
                queuedEvent = null;
                await applyEvent(next); // eslint-disable-line no-await-in-loop -- preserve snapshot order
            }
        } finally {
            applyingEvent = false;
        }
    };
    
    foldersEventSource.onerror = (e) => {
        console.warn('SSE connection error, closing...', e);
        foldersEventSource.close();
        foldersEventSource = null;
        setTimeout(initFoldersSSE, 5000);
    };
}

let quickViewBusy = false;

export function syncQuickViewsUI() {
    const root = dom.sidebarQuickViews;
    if (!root) return;

    const hasSearch = Boolean(dom.searchInput?.value.trim());
    const hasFilters = hasSearch || ratingFilter !== null || !mediaTypeFilter.images || !mediaTypeFilter.videos;
    const reset = document.getElementById('quick-view-reset-btn');
    if (reset) reset.disabled = quickViewBusy || !hasFilters;
    root.querySelectorAll('[data-quick-filter]').forEach(button => { button.disabled = quickViewBusy; });
    const isAll = currentCollection.type === 'media' && !hasFilters;
    const isTopRated = ratingFilter === 5;
    const isUnrated = ratingFilter === 0;
    const isVideosOnly = !mediaTypeFilter.images && mediaTypeFilter.videos;
    const isImagesOnly = mediaTypeFilter.images && !mediaTypeFilter.videos;

    root.querySelectorAll('.quick-chip[data-quick-filter]').forEach(chip => {
        const filter = chip.dataset.quickFilter;
        let active = false;
        if (filter === 'all') active = isAll;
        else if (filter === 'top-rated') active = isTopRated;
        else if (filter === 'unrated') active = isUnrated;
        else if (filter === 'videos') active = isVideosOnly;
        else if (filter === 'images') active = isImagesOnly;
        chip.classList.toggle('active', active);
        chip.setAttribute('aria-pressed', String(active));
    });
}

export function initSidebarAccordion() {
    document.querySelectorAll('.sidebar-section-toggle').forEach(btn => {
        btn.addEventListener('click', event => {
            event.preventDefault();
            const section = btn.closest('.sidebar-section');
            if (section) {
                const collapsed = section.classList.toggle('collapsed');
                btn.setAttribute('aria-expanded', String(!collapsed));
            }
        });
    });

    document.getElementById('folders-add-btn')?.addEventListener('click', event => {
        event.preventDefault();
        document.getElementById('btn-open-folder')?.click();
    });
}

export async function resetViewerFilters({ allMedia = false } = {}) {
    const filtersChanged = ratingFilter !== null || !mediaTypeFilter.images || !mediaTypeFilter.videos;
    setRatingFilter(null);
    setMediaTypeFilter({ images: true, videos: true });
    if (dom.searchInput) dom.searchInput.value = '';
    saveState();
    const [{ syncRatingFilter }, { syncMediaTypeFilter }, { applySearchFilter }] = await Promise.all([
        import('./rating-filter.js'),
        import('./media-type-filter.js'),
        import('../components/search-bar.js'),
    ]);
    syncRatingFilter();
    syncMediaTypeFilter();
    applySearchFilter();
    const { invalidateApiCache, loadCollectionImages, loadMediaCollection } = await import('../api.js');
    invalidateApiCache();
    if (allMedia) await loadMediaCollection({ force: true });
    else if (filtersChanged && isBrowsableCollection(currentCollection)) {
        await loadCollectionImages({ ...currentCollection }, { force: true });
    }
    syncQuickViewsUI();
}

export function initQuickViews() {
    const section = document.getElementById('sidebar-library-section');
    if (!section || !dom.sidebarQuickViews) return;
    syncQuickViewsUI();

    section.addEventListener('click', async event => {
        const chip = event.target.closest('[data-quick-filter]');
        const reset = event.target.closest('#quick-view-reset-btn');
        if ((!chip && !reset) || quickViewBusy) return;
        quickViewBusy = true;
        syncQuickViewsUI();
        try {
            const filter = chip?.dataset.quickFilter;
            if (reset || filter === 'all') {
                await resetViewerFilters({ allMedia: filter === 'all' });
            } else if (filter === 'top-rated' || filter === 'unrated') {
                const { selectRatingFilter } = await import('./rating-filter.js');
                const target = filter === 'top-rated' ? 5 : 0;
                await selectRatingFilter(ratingFilter === target ? null : target);
            } else if (filter === 'videos' || filter === 'images') {
                const { applyMediaTypeFilter } = await import('./media-type-filter.js');
                const alreadyActive = filter === 'videos'
                    ? !mediaTypeFilter.images && mediaTypeFilter.videos
                    : mediaTypeFilter.images && !mediaTypeFilter.videos;
                await applyMediaTypeFilter({
                    images: alreadyActive || filter === 'images',
                    videos: alreadyActive || filter === 'videos',
                });
            }
        } catch (error) {
            showToast(`Could not update filters: ${error.message}`);
        } finally {
            quickViewBusy = false;
            syncQuickViewsUI();
        }
    });
}
