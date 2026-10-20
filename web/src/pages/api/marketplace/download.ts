import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';
import {
    TooManyInstalls,
    allowHeavy,
    getListing,
    issueDownload,
    mayInstall,
    publicListing,
} from '../../../lib/marketplace';
import { hasPaid } from '../../../lib/sales';

/**
 * The pack encrypted for the caller. The key is not included: the app asks
 * for it with /api/marketplace/key each time it starts the pack.
 * Body: { id }.
 */
export const POST: APIRoute = async ({ request }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in to install packs' }, 401);
    if (!(await allowHeavy(user.id))) return json({ error: 'Too many requests. Try again in a minute' }, 429);
    try {
        const { id } = await request.json();
        const listing = await getListing(String(id ?? ''));
        if (!listing) return json({ error: 'This pack is no longer available' }, 404);
        if (!(await mayInstall(user.id, listing, () => hasPaid(user.id, listing.id)))) {
            return json({ error: 'Buy this pack to install it', price: listing.price }, 402);
        }
        const { nonce, body } = await issueDownload(user.id, listing);
        return json({
            listing: publicListing(listing),
            nonce: nonce.toString('base64'),
            ciphertext: body.toString('base64'),
        });
    } catch (error) {
        if (error instanceof TooManyInstalls) {
            return json({ error: 'Too many installs today. Try again tomorrow' }, 429);
        }
        console.error('Error downloading pack:', error);
        return json({ error: 'Could not download the pack' }, 500);
    }
};

export const prerender = false;
