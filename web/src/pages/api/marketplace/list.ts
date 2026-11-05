import type { APIRoute } from 'astro';
import { json } from '../../../lib/auth';
import { listListings, publicListing } from '../../../lib/marketplace';

const PAGE_SIZE = 48;
/** Seconds a page of the list is served from Cloudflare's cache. */
const CACHE_SECONDS = 30;

/**
 * A page of packs, newest first, optionally filtered by a search over title,
 * game, author and description. Query: q, offset.
 */
export const GET: APIRoute = async ({ request, url }) => {
    const cache = (globalThis as any).caches?.default as Cache | undefined;
    const cached = await cache?.match(request);
    if (cached) return cached;
    try {
        const query = (url.searchParams.get('q') ?? '').trim().slice(0, 100);
        const offset = Math.max(0, Math.min(10_000, Number(url.searchParams.get('offset')) || 0));
        // One more than a page, to know whether another page exists.
        const rows = await listListings(query, offset, PAGE_SIZE + 1);
        const response = json(
            {
                listings: rows.slice(0, PAGE_SIZE).map(publicListing),
                nextOffset: rows.length > PAGE_SIZE ? offset + PAGE_SIZE : null,
            },
            200,
            { 'Cache-Control': `public, max-age=${CACHE_SECONDS}` },
        );
        await cache?.put(request, response.clone());
        return response;
    } catch (error) {
        console.error('Error listing marketplace packs:', error);
        return json({ error: 'Could not load the marketplace' }, 500);
    }
};

export const prerender = false;
