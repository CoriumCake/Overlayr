import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';
import { allowHeavy } from '../../../lib/marketplace';
import { accountSession, payableAccount } from '../../../lib/sales';

/** A session for the embedded Stripe payout components, once set up. */
export const POST: APIRoute = async ({ request }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in first' }, 401);
    if (!(await allowHeavy(user.id))) return json({ error: 'Too many requests. Try again in a minute' }, 429);
    try {
        const accountId = await payableAccount(user.id);
        if (!accountId) return json({ error: 'Finish setting up payouts first' }, 409);
        return json({
            clientSecret: await accountSession(accountId),
            publishableKey: process.env.STRIPE_PUBLISHABLE_KEY,
        });
    } catch (error) {
        console.error('Error creating creator session:', error);
        return json({ error: 'Could not open payout setup' }, 500);
    }
};

export const prerender = false;
