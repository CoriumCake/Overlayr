import type { APIRoute } from 'astro';
import { json, refreshDesktop } from '../../../lib/auth';

/**
 * Exchanges the desktop app's refresh token for a new access token and a new
 * refresh token (the old one stops working). Body: { refreshToken }.
 */
export const POST: APIRoute = async ({ request }) => {
    try {
        const { refreshToken } = await request.json();
        if (typeof refreshToken !== 'string' || !refreshToken) {
            return json({ error: 'Missing refresh token' }, 400);
        }
        const login = await refreshDesktop(refreshToken);
        if (!login) return json({ error: 'Log in again' }, 401);
        return json(login);
    } catch (error) {
        console.error('Error refreshing token:', error);
        return json({ error: 'Could not refresh the login' }, 500);
    }
};

export const prerender = false;
