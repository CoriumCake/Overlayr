// Accounts and logins, on Cloudflare D1. Google is the only way to log in.
//
// - Browsers get an HttpOnly session cookie (SameSite=Lax).
// - The desktop app gets a one-hour access token (a JWT signed with
//   AUTH_SECRET, checked without a database query) and a refresh token that
//   is replaced each time it's used.
// Only SHA-256 hashes of session and refresh tokens are stored.
import { env } from 'cloudflare:workers';
import { SignJWT, createRemoteJWKSet, jwtVerify } from 'jose';

export const SESSION_COOKIE = 'ovr_session';
const OAUTH_COOKIE = 'ovr_oauth';
const ACCESS_TOKEN_LIFETIME = '1h';
const SESSION_DAYS = 60;
const ISSUER = 'overlayr';
const AUDIENCE = 'overlayr-api';

const GOOGLE_AUTH = 'https://accounts.google.com/o/oauth2/v2/auth';
const GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token';
const googleKeys = createRemoteJWKSet(new URL('https://www.googleapis.com/oauth2/v3/certs'));

export type User = {
    id: string;
    email: string | null;
    name: string | null;
    avatarUrl: string | null;
};

type UserRow = { id: string; email: string | null; name: string | null; avatar_url: string | null };

function toUser(row: UserRow): User {
    return { id: row.id, email: row.email, name: row.name, avatarUrl: row.avatar_url };
}

function secret(): Uint8Array {
    const value = process.env.AUTH_SECRET;
    if (!value || value.length < 32) throw new Error('AUTH_SECRET is not set (32+ characters)');
    return new TextEncoder().encode(value);
}

function base64url(bytes: Uint8Array): string {
    let text = '';
    for (const byte of bytes) text += String.fromCharCode(byte);
    return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomToken(): string {
    return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

async function sha256(text: string): Promise<string> {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return base64url(new Uint8Array(digest));
}

function now() {
    return new Date().toISOString();
}

function cookie(name: string, value: string, maxAgeSeconds: number) {
    return `${name}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAgeSeconds}`;
}

function readCookie(request: Request, name: string): string | null {
    const header = request.headers.get('cookie') ?? '';
    for (const part of header.split(';')) {
        const [key, ...rest] = part.trim().split('=');
        if (key === name) return rest.join('=');
    }
    return null;
}

/** A same-site path to return to after logging in, or "/" for anything else. */
export function safeReturn(value: string | null | undefined): string {
    if (!value || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/\\')) return '/';
    return value;
}

// ---- Tokens and sessions ----

export async function accessToken(user: User): Promise<string> {
    return new SignJWT({ email: user.email, name: user.name, picture: user.avatarUrl })
        .setProtectedHeader({ alg: 'HS256' })
        .setSubject(user.id)
        .setIssuer(ISSUER)
        .setAudience(AUDIENCE)
        .setIssuedAt()
        .setExpirationTime(ACCESS_TOKEN_LIFETIME)
        .sign(secret());
}

/** Starts a login and returns its token (only its hash is stored). */
export async function createSession(userId: string, kind: 'web' | 'desktop'): Promise<string> {
    const token = randomToken();
    const expires = new Date(Date.now() + SESSION_DAYS * 24 * 60 * 60 * 1000).toISOString();
    await env.DB.prepare(
        'INSERT INTO sessions (id, user_id, kind, token_hash, expires_at) VALUES (?, ?, ?, ?, ?)',
    )
        .bind(crypto.randomUUID(), userId, kind, await sha256(token), expires)
        .run();
    return token;
}

/** Everything the desktop app needs to stay logged in. */
export async function desktopLogin(user: User) {
    return {
        token: await accessToken(user),
        refreshToken: await createSession(user.id, 'desktop'),
        uid: user.id,
    };
}

/**
 * Replaces a desktop refresh token with a new one. A token can be used once:
 * reusing it fails, so a stolen copy stops working once either side uses it.
 */
export async function refreshDesktop(refreshToken: string) {
    const hash = await sha256(refreshToken);
    const row = await env.DB.prepare(
        `SELECT users.id, users.email, users.name, users.avatar_url
         FROM sessions JOIN users ON users.id = sessions.user_id
         WHERE sessions.token_hash = ? AND sessions.kind = 'desktop' AND sessions.expires_at > ?`,
    )
        .bind(hash, now())
        .first<UserRow>();
    if (!row) return null;
    const deleted = await env.DB.prepare('DELETE FROM sessions WHERE token_hash = ?').bind(hash).run();
    // Two refreshes racing with the same token: only one may win.
    if (deleted.meta.changes !== 1) return null;
    return desktopLogin(toUser(row));
}

export async function endSession(token: string) {
    await env.DB.prepare('DELETE FROM sessions WHERE token_hash = ?').bind(await sha256(token)).run();
}

export function sessionCookie(token: string) {
    return cookie(SESSION_COOKIE, token, SESSION_DAYS * 24 * 60 * 60);
}

export function clearedSessionCookie() {
    return cookie(SESSION_COOKIE, '', 0);
}

export function webSessionToken(request: Request) {
    return readCookie(request, SESSION_COOKIE);
}

async function userFromCookie(request: Request): Promise<User | null> {
    const token = webSessionToken(request);
    if (!token) return null;
    const row = await env.DB.prepare(
        `SELECT users.id, users.email, users.name, users.avatar_url
         FROM sessions JOIN users ON users.id = sessions.user_id
         WHERE sessions.token_hash = ? AND sessions.kind = 'web' AND sessions.expires_at > ?`,
    )
        .bind(await sha256(token), now())
        .first<UserRow>();
    return row ? toUser(row) : null;
}

/**
 * The user making the request: the desktop app's `Authorization: Bearer`
 * access token, or a browser's session cookie.
 */
export async function requireUser(request: Request): Promise<User | null> {
    const header = request.headers.get('authorization') ?? '';
    if (header.startsWith('Bearer ')) {
        try {
            const { payload } = await jwtVerify(header.slice(7).trim(), secret(), {
                issuer: ISSUER,
                audience: AUDIENCE,
                algorithms: ['HS256'],
            });
            if (typeof payload.sub !== 'string') return null;
            return {
                id: payload.sub,
                email: typeof payload.email === 'string' ? payload.email : null,
                name: typeof payload.name === 'string' ? payload.name : null,
                avatarUrl: typeof payload.picture === 'string' ? payload.picture : null,
            };
        } catch {
            return null;
        }
    }
    return userFromCookie(request);
}

// ---- Google login ----

function googleClient() {
    const id = process.env.GOOGLE_CLIENT_ID;
    const clientSecret = process.env.GOOGLE_CLIENT_SECRET;
    if (!id || !clientSecret) throw new Error('GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set');
    return { id, secret: clientSecret };
}

function redirectUri(request: Request) {
    return `${new URL(request.url).origin}/api/auth/callback`;
}

/**
 * Where to send the browser to log in with Google, and a short-lived signed
 * cookie holding the state, PKCE verifier and nonce to check on the way back.
 * `returnTo` is a site path, or "desktop" to hand the login to the app.
 */
export async function startGoogleLogin(request: Request, returnTo: string) {
    const state = randomToken();
    const verifier = randomToken();
    const nonce = randomToken();
    const challenge = await sha256(verifier);
    const flow = await new SignJWT({ state, verifier, nonce, returnTo })
        .setProtectedHeader({ alg: 'HS256' })
        .setAudience('overlayr-oauth')
        .setExpirationTime('10m')
        .sign(secret());
    const url = new URL(GOOGLE_AUTH);
    url.search = new URLSearchParams({
        client_id: googleClient().id,
        redirect_uri: redirectUri(request),
        response_type: 'code',
        scope: 'openid email profile',
        state,
        nonce,
        code_challenge: challenge,
        code_challenge_method: 'S256',
        prompt: 'select_account',
    }).toString();
    return { url: url.toString(), cookie: cookie(OAUTH_COOKIE, flow, 600) };
}

export const clearedOauthCookie = () => cookie(OAUTH_COOKIE, '', 0);

/** Finishes a Google login: checks it, and creates or updates the user. */
export async function finishGoogleLogin(request: Request): Promise<{ user: User; returnTo: string }> {
    const params = new URL(request.url).searchParams;
    const flowCookie = readCookie(request, OAUTH_COOKIE);
    if (!flowCookie) throw new Error('The login took too long. Try again.');
    const { payload: flow } = await jwtVerify(flowCookie, secret(), { audience: 'overlayr-oauth' });
    if (params.get('error')) throw new Error('Google login was cancelled.');
    if (!params.get('code') || params.get('state') !== flow.state) throw new Error('Invalid login. Try again.');

    const client = googleClient();
    const response = await fetch(GOOGLE_TOKEN, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
            code: params.get('code')!,
            client_id: client.id,
            client_secret: client.secret,
            redirect_uri: redirectUri(request),
            grant_type: 'authorization_code',
            code_verifier: String(flow.verifier),
        }),
    });
    const tokens = (await response.json()) as { id_token?: string };
    if (!response.ok || !tokens.id_token) throw new Error('Google did not confirm the login.');
    const { payload: google } = await jwtVerify(tokens.id_token, googleKeys, {
        issuer: ['https://accounts.google.com', 'accounts.google.com'],
        audience: client.id,
    });
    if (google.nonce !== flow.nonce || typeof google.sub !== 'string') throw new Error('Invalid login. Try again.');
    if (google.email && google.email_verified === false) throw new Error('Verify your Google email first.');

    const user = await upsertGoogleUser(
        google.sub,
        typeof google.email === 'string' ? google.email : null,
        typeof google.name === 'string' ? google.name : null,
        typeof google.picture === 'string' ? google.picture : null,
    );
    return { user, returnTo: String(flow.returnTo) };
}

async function upsertGoogleUser(sub: string, email: string | null, name: string | null, picture: string | null) {
    const id = crypto.randomUUID();
    const row = await env.DB.prepare(
        `INSERT INTO users (id, google_sub, email, name, avatar_url) VALUES (?, ?, ?, ?, ?)
         ON CONFLICT (google_sub) DO UPDATE SET
             email = excluded.email, name = excluded.name, avatar_url = excluded.avatar_url
         RETURNING id, email, name, avatar_url`,
    )
        .bind(id, sub, email, name, picture)
        .first<UserRow>();
    if (!row) throw new Error('Could not save the account.');
    return toUser(row);
}

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers },
    });
}
