import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';
import { creatorStatus } from '../../../lib/sales';

/** Whether the caller can sell packs, and their sales so far. */
export const GET: APIRoute = async ({ request }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in first' }, 401);
    try {
        return json(await creatorStatus(user.id));
    } catch (error) {
        console.error('Error loading creator status:', error);
        return json({ error: 'Could not load your creator account' }, 500);
    }
};

export const prerender = false;
