import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';
import { ownedPacks } from '../../../lib/sales';

/** IDs of the packs the caller bought. */
export const GET: APIRoute = async ({ request }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in first' }, 401);
    try {
        return json({ packs: await ownedPacks(user.id) });
    } catch (error) {
        console.error('Error listing purchases:', error);
        return json({ error: 'Could not load your purchases' }, 500);
    }
};

export const prerender = false;
