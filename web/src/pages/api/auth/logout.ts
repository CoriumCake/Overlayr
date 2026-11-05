import type { APIRoute } from 'astro';
import { clearedSessionCookie, endSession, json, webSessionToken } from '../../../lib/auth';

/**
 * Ends a login: the browser's session cookie, and/or the desktop app's
 * refresh token from the body ({ refreshToken }).
 */
export const POST: APIRoute = async ({ request }) => {
    const cookieToken = webSessionToken(request);
    if (cookieToken) await endSession(cookieToken);
    const body = await request.json().catch(() => ({}));
    if (typeof body?.refreshToken === 'string' && body.refreshToken) await endSession(body.refreshToken);
    return json({ ok: true }, 200, { 'Set-Cookie': clearedSessionCookie() });
};

export const prerender = false;
