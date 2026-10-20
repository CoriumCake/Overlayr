import type { APIRoute } from 'astro';
import { getCover } from '../../../../lib/marketplace';

/** A pack's cover picture. Each upload gets a new name, so it never changes. */
export const GET: APIRoute = async ({ params }) => {
    const cover = await getCover(params.name ?? '');
    if (!cover) return new Response('Not found', { status: 404 });
    return new Response(cover.body, {
        headers: {
            'Content-Type': 'image/png',
            'Cache-Control': 'public, max-age=31536000, immutable',
            ETag: cover.httpEtag,
        },
    });
};

export const prerender = false;
