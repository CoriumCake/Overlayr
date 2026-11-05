import type { APIRoute } from 'astro';
import { safeReturn, startGoogleLogin } from '../../../lib/auth';

/** Starts a Google login. Query: return (a site path, or "desktop"). */
export const GET: APIRoute = async ({ request, url }) => {
    const requested = url.searchParams.get('return');
    const returnTo = requested === 'desktop' ? 'desktop' : safeReturn(requested);
    const { url: google, cookie } = await startGoogleLogin(request, returnTo);
    return new Response(null, { status: 302, headers: { Location: google, 'Set-Cookie': cookie } });
};

export const prerender = false;
