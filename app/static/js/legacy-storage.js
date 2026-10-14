/**
 * One-time move of browser storage written under the pre-codename key prefix.
 * Loaded as a classic script before any page code reads its settings, so renamed keys
 * (theme, preferences, Create session, ...) keep the values users already saved.
 */
(function () {
    const LEGACY_PREFIX = 'cmv_';
    const PREFIX = 'mediastudio_';

    function migrate(storage) {
        if (!storage) return;
        const legacyKeys = [];
        for (let i = 0; i < storage.length; i += 1) {
            const key = storage.key(i);
            if (key && key.startsWith(LEGACY_PREFIX)) legacyKeys.push(key);
        }
        for (const key of legacyKeys) {
            const target = PREFIX + key.slice(LEGACY_PREFIX.length);
            if (storage.getItem(target) === null) storage.setItem(target, storage.getItem(key));
            storage.removeItem(key);
        }
    }

    function run() {
        for (const name of ['localStorage', 'sessionStorage']) {
            try { migrate(globalThis[name]); } catch (_e) { /* storage unavailable */ }
        }
    }

    globalThis.__migrateLegacyStorage = run;
    run();
})();
