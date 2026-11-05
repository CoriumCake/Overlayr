import type { APIRoute } from 'astro';
import { desktopLogin, json, requireUser } from '../../../lib/auth';

/**
 * Logs the desktop app in as the browser's user without another Google
 * round trip. Needs the browser's session cookie, so other sites can't call
 * it (SameSite cookie, and Astro rejects cross-site form posts).
 */
export const POST: APIRoute = async ({ request }) => {
    if (request.headers.get('authorization')) return json({ error: 'Use the browser session' }, 400);
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in first' }, 401);
    return json(await desktopLogin(user));
};

export const prerender = false;
