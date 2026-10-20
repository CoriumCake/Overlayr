import type { APIRoute } from 'astro';
import {
    clearedOauthCookie,
    createSession,
    desktopLogin,
    finishGoogleLogin,
    safeReturn,
    sessionCookie,
} from '../../../lib/auth';

/**
 * Google sends the browser back here. A desktop login goes to the login page
 * with the app's tokens in the URL fragment (never sent to any server), which
 * passes them to the app; a web login gets a session cookie.
 */
export const GET: APIRoute = async ({ request }) => {
    const headers = new Headers();
    headers.append('Set-Cookie', clearedOauthCookie());
    try {
        const { user, returnTo } = await finishGoogleLogin(request);
        if (returnTo === 'desktop') {
            const login = await desktopLogin(user);
            const fragment = new URLSearchParams({
                token: login.token,
                uid: login.uid,
                refresh: login.refreshToken,
            });
            headers.set('Location', `/login?callback=desktop#${fragment}`);
        } else {
            headers.append('Set-Cookie', sessionCookie(await createSession(user.id, 'web')));
            headers.set('Location', safeReturn(returnTo));
        }
    } catch (error: any) {
        console.error('Login failed:', error);
        const message = error instanceof Error ? error.message : 'Login failed. Try again.';
        headers.set('Location', `/login?error=${encodeURIComponent(message)}`);
    }
    return new Response(null, { status: 302, headers });
};

export const prerender = false;
