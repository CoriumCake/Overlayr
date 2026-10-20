import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';
import { deletePack, getListing, hidePack } from '../../../lib/marketplace';
import { hasSales } from '../../../lib/sales';

/**
 * Removes the caller's own pack. Installed free copies stop unlocking; a pack
 * people paid for is only taken out of the list, so its buyers keep it.
 * Body: { id }.
 */
export const POST: APIRoute = async ({ request }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in first' }, 401);
    try {
        const { id } = await request.json();
        const listing = await getListing(String(id ?? ''));
        if (!listing) return json({ error: 'This pack is no longer available' }, 404);
        if (listing.owner_id !== user.id) return json({ error: 'Only its creator can remove a pack' }, 403);
        if (await hasSales(listing.id)) await hidePack(listing);
        else await deletePack(listing);
        return json({ removed: true });
    } catch (error) {
        console.error('Error removing pack:', error);
        return json({ error: 'Could not remove the pack' }, 500);
    }
};

export const prerender = false;
