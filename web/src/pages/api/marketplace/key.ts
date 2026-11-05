import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';
import { getListing, licenseKey } from '../../../lib/marketplace';

/**
 * The caller's licence key for an installed pack version. Answers 409 with
 * the current version when the installed copy is outdated, so the app can
 * download it again. Body: { id, version }.
 */
export const POST: APIRoute = async ({ request }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in to use marketplace packs' }, 401);
    try {
        const { id, version } = await request.json();
        const listing = await getListing(String(id ?? ''));
        if (!listing) return json({ error: 'This pack was removed from the marketplace' }, 404);
        const key =
            listing.version === version ? await licenseKey(user.id, listing.id, listing.version) : null;
        if (!key) return json({ error: 'outdated', version: listing.version }, 409);
        return json({ key: key.toString('base64') });
    } catch (error) {
        console.error('Error issuing pack key:', error);
        return json({ error: 'Could not unlock the pack' }, 500);
    }
};

export const prerender = false;
