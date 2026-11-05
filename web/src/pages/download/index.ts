import type { APIRoute } from 'astro';
import { platformFromUserAgent } from '../../lib/releases';

/** Sends visitors to the installer for the system they're on. */
export const GET: APIRoute = async ({ request, redirect }) => {
    const platform = platformFromUserAgent(request.headers.get('user-agent') ?? '');
    return redirect(platform ? `/download/${platform}` : '/downloads', 302);
};

export const prerender = false;
