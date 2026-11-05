// Server side of the translation pack marketplace.
//
// Packs are stored encrypted and never leave the server in plain text:
// - each pack has a master key in D1 (`marketplace_keys`), and its ciphertext
//   in the R2 bucket (Worker reads from R2 cost no bandwidth);
// - a download re-encrypts the pack under a fresh key for that user, saved as
//   a licence in `marketplace_licenses`, so a leaked file and key can be
//   traced to one account and revoked;
// - the desktop app asks for its licence key each time it starts a pack and
//   decrypts the pack in memory only.
import { Buffer } from 'node:buffer';
import { env } from 'cloudflare:workers';

/** Largest pack accepted, in bytes of JSON. */
export const MAX_PACK_BYTES = 3_000_000;
export const MAX_COVER_BYTES = 400_000;
/** New installs one account may make per day. */
const DAILY_INSTALLS = 200;

export type ListingRow = {
    id: string;
    owner_id: string;
    title: string;
    description: string;
    application: string;
    source_lang: string;
    target_lang: string;
    entries: number;
    author: string;
    /** R2 key of the cover picture, empty without one. */
    cover_key: string;
    /** Satang; 0 is free. */
    price: number;
    /** 1 when removed by its creator but kept for the people who bought it. */
    hidden: number;
    version: number;
    downloads: number;
    size: number;
    created_at: string;
    updated_at: string;
};

/** What the desktop app shows; never includes keys or content. */
export function publicListing(row: ListingRow) {
    return {
        id: row.id,
        title: row.title,
        description: row.description,
        application: row.application,
        sourceLang: row.source_lang,
        targetLang: row.target_lang,
        entries: row.entries,
        author: row.author,
        ownerUid: row.owner_id,
        // Relative to the site; the cover's key changes with each upload, so
        // it can be cached forever.
        coverUrl: row.cover_key ? `/api/marketplace/cover/${row.cover_key.replace(/^covers\//, '')}` : '',
        price: row.price,
        currency: 'thb',
        version: row.version,
        downloads: row.downloads,
        size: row.size,
        createdAt: row.created_at,
        updatedAt: row.updated_at,
    };
}

/** AES-256-GCM with WebCrypto; the ciphertext ends with the 16-byte tag. */
async function encrypt(key: Uint8Array, plain: Uint8Array, aad: string) {
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const cryptoKey = await crypto.subtle.importKey('raw', key as BufferSource, 'AES-GCM', false, ['encrypt']);
    const body = await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv: nonce, additionalData: new TextEncoder().encode(aad) },
        cryptoKey,
        plain as BufferSource,
    );
    return { nonce: Buffer.from(nonce), body: Buffer.from(body) };
}

async function decrypt(key: Uint8Array, nonce: Uint8Array, sealed: Uint8Array, aad: string) {
    const cryptoKey = await crypto.subtle.importKey('raw', key as BufferSource, 'AES-GCM', false, ['decrypt']);
    const plain = await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: nonce as BufferSource, additionalData: new TextEncoder().encode(aad) },
        cryptoKey,
        sealed as BufferSource,
    );
    return new Uint8Array(plain);
}

/** Additional data binding a ciphertext to one pack version. */
function packAad(id: string, version: number) {
    return `overlayr-pack:${id}:${version}`;
}

export function validId(id: string) {
    return /^[A-Za-z0-9_-]{1,64}$/.test(id);
}

const packKey = (id: string) => `packs/${id}.bin`;

export async function getListing(id: string): Promise<ListingRow | null> {
    if (!validId(id)) return null;
    return env.DB.prepare('SELECT * FROM marketplace_listings WHERE id = ?').bind(id).first<ListingRow>();
}

/** A page of listings, newest first, optionally filtered by `query`. */
export async function listListings(query: string, offset: number, limit: number) {
    if (!query) {
        const { results } = await env.DB.prepare(
            'SELECT * FROM marketplace_listings WHERE hidden = 0 ORDER BY updated_at DESC LIMIT ? OFFSET ?',
        )
            .bind(limit, offset)
            .all<ListingRow>();
        return results;
    }
    // LIKE patterns are limited to 50 bytes in D1.
    const term = query.slice(0, 40).replace(/[\\%_]/g, (character) => `\\${character}`);
    const pattern = `%${term}%`;
    const { results } = await env.DB.prepare(
        `SELECT * FROM marketplace_listings
         WHERE hidden = 0 AND (title LIKE ?1 ESCAPE '\\' OR application LIKE ?1 ESCAPE '\\'
            OR author LIKE ?1 ESCAPE '\\' OR description LIKE ?1 ESCAPE '\\')
         ORDER BY updated_at DESC LIMIT ?2 OFFSET ?3`,
    )
        .bind(pattern, limit, offset)
        .all<ListingRow>();
    return results;
}

async function masterKey(id: string): Promise<Buffer | null> {
    const row = await env.DB.prepare('SELECT key FROM marketplace_keys WHERE pack_id = ?')
        .bind(id)
        .first<{ key: string }>();
    return row ? Buffer.from(row.key, 'base64') : null;
}

/** Stores a cover picture under a new key and returns it. */
export async function saveCover(id: string, version: number, png: Buffer) {
    const key = `covers/${id}-${version}.png`;
    await env.PACKS.put(key, png, {
        httpMetadata: { contentType: 'image/png', cacheControl: 'public, max-age=31536000, immutable' },
    });
    return key;
}

export async function getCover(name: string) {
    if (!/^[A-Za-z0-9_-]{1,64}-\d+\.png$/.test(name)) return null;
    return env.PACKS.get(`covers/${name}`);
}

/** Saves the listing and the pack, encrypted under the pack's master key. */
export async function savePack(row: ListingRow, plain: Buffer) {
    await env.DB.prepare(
        `INSERT INTO marketplace_listings (id, owner_id, title, description, application, source_lang,
             target_lang, entries, author, cover_key, price, hidden, version, downloads, size, created_at,
             updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT (id) DO UPDATE SET
             title = excluded.title, description = excluded.description,
             application = excluded.application, source_lang = excluded.source_lang,
             target_lang = excluded.target_lang, entries = excluded.entries, author = excluded.author,
             cover_key = excluded.cover_key, price = excluded.price, hidden = excluded.hidden, version = excluded.version, size = excluded.size,
             updated_at = excluded.updated_at`,
    )
        .bind(
            row.id,
            row.owner_id,
            row.title,
            row.description,
            row.application,
            row.source_lang,
            row.target_lang,
            row.entries,
            row.author,
            row.cover_key,
            row.price,
            row.hidden,
            row.version,
            row.downloads,
            row.size,
            row.created_at,
            row.updated_at,
        )
        .run();
    let master = await masterKey(row.id);
    if (!master) {
        master = Buffer.from(crypto.getRandomValues(new Uint8Array(32)));
        await env.DB.prepare('INSERT INTO marketplace_keys (pack_id, key) VALUES (?, ?)')
            .bind(row.id, master.toString('base64'))
            .run();
    }
    const { nonce, body } = await encrypt(master, plain, packAad(row.id, row.version));
    await env.PACKS.put(packKey(row.id), Buffer.concat([nonce, body]), {
        httpMetadata: { contentType: 'application/octet-stream' },
    });
}

async function loadPack(row: ListingRow): Promise<Uint8Array> {
    const master = await masterKey(row.id);
    if (!master) throw new Error(`pack ${row.id} has no key`);
    const object = await env.PACKS.get(packKey(row.id));
    if (!object) throw new Error(`pack ${row.id} has no content`);
    const stored = new Uint8Array(await object.arrayBuffer());
    return decrypt(master, stored.subarray(0, 12), stored.subarray(12), packAad(row.id, row.version));
}

export class TooManyInstalls extends Error {}

/** Encrypts the pack for one user under a fresh licence key. */
export async function issueDownload(userId: string, row: ListingRow) {
    const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const recent = await env.DB.prepare(
        'SELECT COUNT(*) AS count FROM marketplace_licenses WHERE user_id = ? AND issued_at >= ?',
    )
        .bind(userId, since)
        .first<{ count: number }>();
    if ((recent?.count ?? 0) >= DAILY_INSTALLS) throw new TooManyInstalls();

    const plain = await loadPack(row);
    const key = crypto.getRandomValues(new Uint8Array(32));
    const { nonce, body } = await encrypt(key, plain, packAad(row.id, row.version));
    plain.fill(0);
    await env.DB.batch([
        env.DB.prepare(
            `INSERT INTO marketplace_licenses (user_id, pack_id, version, key, revoked, issued_at)
             VALUES (?, ?, ?, ?, 0, ?)
             ON CONFLICT (user_id, pack_id) DO UPDATE SET
                 version = excluded.version, key = excluded.key, revoked = 0, issued_at = excluded.issued_at`,
        ).bind(userId, row.id, row.version, Buffer.from(key).toString('base64'), new Date().toISOString()),
        env.DB.prepare('UPDATE marketplace_listings SET downloads = downloads + 1 WHERE id = ?').bind(row.id),
    ]);
    return { nonce, body };
}

/**
 * Whether the user may download the pack: it's free, theirs, paid for, or
 * they installed it while it was free (a licence that wasn't revoked).
 */
export async function mayInstall(userId: string, row: ListingRow, paid: () => Promise<boolean>) {
    if (row.owner_id === userId) return true;
    if (row.hidden === 0 && row.price === 0) return true;
    const licence = await env.DB.prepare(
        'SELECT revoked FROM marketplace_licenses WHERE user_id = ? AND pack_id = ?',
    )
        .bind(userId, row.id)
        .first<{ revoked: number }>();
    if (licence && !licence.revoked) return true;
    return paid();
}

/** The user's licence key for a downloaded pack version, if still valid. */
export async function licenseKey(userId: string, id: string, version: number) {
    const row = await env.DB.prepare(
        'SELECT key, version, revoked FROM marketplace_licenses WHERE user_id = ? AND pack_id = ?',
    )
        .bind(userId, id)
        .first<{ key: string; version: number; revoked: number }>();
    if (!row || row.revoked || row.version !== version) return null;
    return Buffer.from(row.key, 'base64');
}

/** Takes a pack out of the list but keeps it for the people who bought it. */
export async function hidePack(row: ListingRow) {
    await env.DB.prepare('UPDATE marketplace_listings SET hidden = 1 WHERE id = ?').bind(row.id).run();
}

/** Removes a pack; keys and licences go with it (foreign keys cascade). */
export async function deletePack(row: ListingRow) {
    await env.PACKS.delete([packKey(row.id), ...(row.cover_key ? [row.cover_key] : [])]);
    await env.DB.prepare('DELETE FROM marketplace_listings WHERE id = ?').bind(row.id).run();
}

/** Per-account limit for expensive requests (installs and publishes). */
export async function allowHeavy(userId: string) {
    if (!env.HEAVY_LIMIT) return true;
    const { success } = await env.HEAVY_LIMIT.limit({ key: userId });
    return success;
}
