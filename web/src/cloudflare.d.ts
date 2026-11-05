// The parts of the Cloudflare Workers runtime the API routes use. Declared
// here instead of through @cloudflare/workers-types, whose globals clash with
// the DOM types the page scripts need.
declare module 'cloudflare:workers' {
    interface R2Object {
        arrayBuffer(): Promise<ArrayBuffer>;
        json<T>(): Promise<T>;
        body: ReadableStream;
        httpEtag: string;
        size: number;
    }
    interface R2Bucket {
        get(key: string): Promise<R2Object | null>;
        put(
            key: string,
            value: ArrayBuffer | ArrayBufferView | string,
            options?: { httpMetadata?: { contentType?: string; cacheControl?: string } },
        ): Promise<unknown>;
        delete(keys: string | string[]): Promise<void>;
    }
    interface RateLimit {
        limit(options: { key: string }): Promise<{ success: boolean }>;
    }
    interface D1Result<T> {
        results: T[];
        meta: { changes: number };
    }
    interface D1PreparedStatement {
        bind(...values: unknown[]): D1PreparedStatement;
        first<T = Record<string, unknown>>(): Promise<T | null>;
        all<T = Record<string, unknown>>(): Promise<D1Result<T>>;
        run(): Promise<D1Result<unknown>>;
    }
    interface D1Database {
        prepare(query: string): D1PreparedStatement;
        batch(statements: D1PreparedStatement[]): Promise<D1Result<unknown>[]>;
    }
    export const env: {
        DB: D1Database;
        PACKS: R2Bucket;
        RELEASES: R2Bucket;
        API_LIMIT?: RateLimit;
        HEAVY_LIMIT?: RateLimit;
        TRANSLATE_LIMIT?: RateLimit;
        AI?: { run(model: string, input: unknown): Promise<unknown> };
        [name: string]: unknown;
    };
}
