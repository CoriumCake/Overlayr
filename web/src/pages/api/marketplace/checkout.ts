import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';
import { allowHeavy, getListing } from '../../../lib/marketplace';
import { NotForSale, startCheckout } from '../../../lib/sales';

/** Starts buying a pack; answers with the Stripe Checkout URL. Body: { id }. */
export const POST: APIRoute = async ({ request, url }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in to buy packs' }, 401);
    if (!(await allowHeavy(user.id))) return json({ error: 'Too many requests. Try again in a minute' }, 429);
    try {
        const { id } = await request.json();
        const listing = await getListing(String(id ?? ''));
        if (!listing) return json({ error: 'This pack is no longer available' }, 404);
        return json({ url: await startCheckout(user, listing, url.origin) });
    } catch (error) {
        if (error instanceof NotForSale) return json({ error: error.message }, 409);
        console.error('Error starting checkout:', error);
        return json({ error: 'Could not start the payment' }, 500);
    }
};

export const prerender = false;
