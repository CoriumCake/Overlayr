import type { APIRoute } from 'astro';
import Stripe from 'stripe';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY as string, {
    apiVersion: '2025-01-27.acacia' as any,
});

export const POST: APIRoute = async ({ request }) => {
    try {
        const { subscriptionId } = await request.json();

        if (!subscriptionId) {
            return new Response(JSON.stringify({ error: 'Missing subscriptionId' }), { status: 400 });
        }

        const subscription = await stripe.subscriptions.retrieve(subscriptionId, {
            expand: ['items.data.price.product'],
        });

        const item = subscription.items.data[0];
        const price = item?.price;
        const product = price?.product as Stripe.Product | undefined;

        return new Response(JSON.stringify({
            status: subscription.status,
            cancelAtPeriodEnd: subscription.cancel_at_period_end,
            cancelAt: subscription.cancel_at ? new Date(subscription.cancel_at * 1000).toISOString() : null,
            currentPeriodEnd: new Date(subscription.current_period_end * 1000).toISOString(),
            currentPeriodStart: new Date(subscription.current_period_start * 1000).toISOString(),
            interval: price?.recurring?.interval || null,
            intervalCount: price?.recurring?.interval_count || null,
            productName: product?.name || null,
            amount: price?.unit_amount || null,
            currency: price?.currency || null,
        }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
        });
    } catch (error: any) {
        console.error('Error fetching subscription details:', error);
        return new Response(JSON.stringify({ error: 'Failed to fetch subscription details' }), { status: 500 });
    }
};

export const prerender = false;
