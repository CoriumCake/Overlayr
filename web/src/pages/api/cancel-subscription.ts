import type { APIRoute } from 'astro';
import Stripe from 'stripe';
import { db } from '../../lib/firebase-admin';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY as string, {
    apiVersion: '2025-01-27.acacia' as any,
});

export const POST: APIRoute = async ({ request }) => {
    try {
        const { subscriptionId, uid } = await request.json();

        if (!subscriptionId) {
            return new Response(JSON.stringify({ error: 'Missing subscriptionId' }), { status: 400 });
        }

        // Cancel at end of current billing period (user keeps access until then)
        const subscription = await stripe.subscriptions.update(subscriptionId, {
            cancel_at_period_end: true,
        });

        const cancelAt = new Date(subscription.current_period_end * 1000).toISOString();

        // Update Firestore with cancellation info
        if (uid) {
            await db.collection('users').doc(uid).update({
                cancelAtPeriodEnd: true,
                cancelAt: cancelAt,
                updatedAt: new Date().toISOString(),
            });
        }

        return new Response(JSON.stringify({
            success: true,
            cancelAt: cancelAt,
            status: subscription.status,
        }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
        });
    } catch (error: any) {
        console.error('Error cancelling subscription:', error);
        return new Response(JSON.stringify({ error: 'Failed to cancel subscription' }), { status: 500 });
    }
};

export const prerender = false;
