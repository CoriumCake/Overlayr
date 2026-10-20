import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';
import { allowHeavy } from '../../../lib/marketplace';
import { accountLink, ensureCreatorAccount, payableAccount } from '../../../lib/sales';

/**
 * Creates the caller's creator account on first use, and answers with the
 * Stripe page to set it up, or to update it once it can sell.
 */
export const POST: APIRoute = async ({ request, url }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in first' }, 401);
    if (!(await allowHeavy(user.id))) return json({ error: 'Too many requests. Try again in a minute' }, 429);
    try {
        const accountId = await ensureCreatorAccount(user);
        const ready = Boolean(await payableAccount(user.id));
        return json({ url: await accountLink(accountId, url.origin, ready) });
    } catch (error) {
        console.error('Error creating onboarding link:', error);
        return json({ error: 'Could not open payout setup' }, 500);
    }
};

export const prerender = false;
