import type { APIRoute } from 'astro';
import { env } from 'cloudflare:workers';

/**
 * One version's installer: /releases/0.1.2/Overlayr-0.1.2-linux-x86_64.tar.gz.
 * Unlike /download/<platform> it never changes, so package managers (the
 * AUR's overlayr-bin) can pin it with a checksum.
 */
export const GET: APIRoute = async ({ params }) => {
    const version = params.version ?? '';
    const file = params.file ?? '';
    if (!/^\d+\.\d+\.\d+$/.test(version) || !/^Overlayr-[\w.-]+$/.test(file)) {
        return new Response('Not found', { status: 404 });
    }
    const object = await env.RELEASES.get(`v${version}/${file}`);
    if (!object) return new Response('Not found', { status: 404 });
    return new Response(object.body, {
        headers: {
            'Content-Type': 'application/octet-stream',
            'Content-Length': String(object.size),
            'Content-Disposition': `attachment; filename="${file}"`,
            ETag: object.httpEtag,
            'Cache-Control': 'public, max-age=31536000, immutable',
        },
    });
};

export const prerender = false;
