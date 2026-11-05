// Browser-side login state. The session is an HttpOnly cookie that page
// scripts can't read; they ask /api/auth/me who is logged in.

/** The fields the pages use for the logged-in user. */
export type AppUser = {
    uid: string;
    email: string | null;
    displayName: string | null;
    photoURL: string | null;
};

let current: AppUser | null = null;
let loaded: Promise<AppUser | null> | null = null;
const listeners = new Set<(user: AppUser | null) => void>();

function load(): Promise<AppUser | null> {
    loaded ??= fetch('/api/auth/me', { credentials: 'same-origin' })
        .then((response) => (response.ok ? response.json() : { user: null }))
        .then(({ user }) => {
            current = user
                ? { uid: user.id, email: user.email, displayName: user.name, photoURL: user.avatarUrl }
                : null;
            return current;
        })
        .catch(() => null);
    return loaded;
}

export function currentUser(): AppUser | null {
    return current;
}

/** Calls `callback` with the logged-in user once known, and on logout. */
export function onUser(callback: (user: AppUser | null) => void) {
    listeners.add(callback);
    load().then((user) => {
        if (listeners.has(callback)) callback(user);
    });
    return () => listeners.delete(callback);
}

/** Logs in with Google and comes back to `returnTo` (default: this page). */
export function signInWithGoogle(returnTo = window.location.pathname + window.location.search) {
    window.location.href = `/api/auth/google?return=${encodeURIComponent(returnTo)}`;
}

export async function signOutUser() {
    await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
    });
    current = null;
    loaded = Promise.resolve(null);
    for (const listener of listeners) listener(null);
}

/** POSTs JSON to one of the site's API routes as the logged-in user. */
export function postAsUser(path: string, body: unknown = {}) {
    return fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
}
