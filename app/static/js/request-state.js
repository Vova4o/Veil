/** Cache ownership survives invalidation and out-of-order request completion. */
export class JsonRequestCache {
    constructor(fetcher = (...args) => fetch(...args)) {
        this.fetcher = fetcher;
        this.responses = new Map();
        this.pending = new Map();
        this.generation = 0;
    }

    invalidate() {
        this.generation++;
        this.responses.clear();
        this.pending.clear();
    }

    fetch(url, { force = false, options } = {}) {
        const method = options?.method || 'GET';
        if (method === 'GET' && !force) {
            if (this.responses.has(url)) return Promise.resolve(this.responses.get(url));
            if (this.pending.has(url)) return this.pending.get(url);
        }
        const generation = this.generation;
        const request = this.fetcher(url, options).then(async response => {
            const data = await response.json().catch(() => ({}));
            if (!response.ok || data.error) {
                throw new Error(data.error || `${response.status} ${response.statusText}`);
            }
            if (method === 'GET' && this.generation === generation && this.pending.get(url) === request) {
                this.responses.set(url, data);
            }
            return data;
        }).finally(() => {
            if (this.pending.get(url) === request) this.pending.delete(url);
        });
        if (method === 'GET') this.pending.set(url, request);
        return request;
    }
}

export class LatestRequest {
    constructor() { this.generation = 0; }
    begin() { return ++this.generation; }
    owns(token) { return this.generation === token; }
}
