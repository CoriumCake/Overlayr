import type { APIRoute } from 'astro';
import { isPlatform, latestDownload } from '../../lib/releases';

/** The latest installer for a platform: /download/windows, macos or linux. */
export const GET: APIRoute = async ({ params, redirect }) => {
    const platform = params.platform ?? '';
    if (!isPlatform(platform)) return redirect('/downloads', 302);
    const download = await latestDownload(platform);
    // No release for this platform yet: the downloads page explains.
    if (!download) return redirect('/downloads', 302);
    return new Response(download.object.body, {
        headers: {
            'Content-Type': 'application/octet-stream',
            'Content-Length': String(download.object.size),
            'Content-Disposition': `attachment; filename="${download.file.name}"`,
            ETag: download.object.httpEtag,
            // Short, so a new release is picked up quickly.
            'Cache-Control': 'public, max-age=300',
        },
    });
};

export const prerender = false;
