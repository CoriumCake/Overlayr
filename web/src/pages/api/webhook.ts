import type { APIRoute } from 'astro';
import Stripe from 'stripe';
import { db } from '../../lib/firebase-admin';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY as string, {
    apiVersion: '2025-01-27.acacia' as any,
});

const endpointSecret = process.env.STRIPE_WEBHOOK_SECRET;

export const POST: APIRoute = async ({ request }) => {
    const signature = request.headers.get('stripe-signature');

    if (!signature || !endpointSecret) {
        return new Response('Webhook signature or secret missing', { status: 400 });
    }

    const body = await request.text();
    let event: Stripe.Event;

    try {
        event = stripe.webhooks.constructEvent(body, signature, endpointSecret);
    } catch (err: any) {
        console.error(`Webhook signature verification failed: ${err.message}`);
        return new Response(`Webhook Error: ${err.message}`, { status: 400 });
    }

    console.log(`Processing event: ${event.type}`);

    try {
        switch (event.type) {
            case 'checkout.session.completed': {
                const session = event.data.object as Stripe.Checkout.Session;
                const uid = session.client_reference_id;
                const customerId = session.customer as string;

                if (!uid) {
                    console.error('No client_reference_id (UID) found in session');
                    break;
                }

                // Fetch active entitlements from Stripe
                const entitlements = await stripe.entitlements.activeEntitlements.list({
                    customer: customerId,
                });

                const features = entitlements.data.map((e) => e.feature.lookup_key);

                await db.collection('users').doc(uid).set({
                    stripeCustomerId: customerId,
                    isPro: true,
                    plan: session.metadata?.plan || 'pro',
                    features: features,
                    subscriptionId: session.subscription as string,
                    updatedAt: new Date().toISOString(),
                }, { merge: true });

                console.log(`Updated features for user ${uid}: ${features.join(', ')}`);
                break;
            }

            case 'customer.subscription.updated':
            case 'customer.subscription.deleted': {
                const subscription = event.data.object as Stripe.Subscription;
                const customerId = subscription.customer as string;

                // Find user by stripeCustomerId
                const userSnapshot = await db.collection('users')
                    .where('stripeCustomerId', '==', customerId)
                    .limit(1)
                    .get();

                if (userSnapshot.empty) {
                    console.error(`No user found for Stripe Customer ID: ${customerId}`);
                    break;
                }

                const userDoc = userSnapshot.docs[0];
                const uid = userDoc.id;

                if (event.type === 'customer.subscription.deleted') {
                    await userDoc.ref.update({
                        isPro: false,
                        features: [],
                        updatedAt: new Date().toISOString(),
                    });
                    console.log(`Revoked features for user ${uid} due to subscription deletion`);
                } else {
                    // Update entitlements summary
                    const entitlements = await stripe.entitlements.activeEntitlements.list({
                        customer: customerId,
                    });
                    const features = entitlements.data.map((e) => e.feature.lookup_key);

                    await userDoc.ref.update({
                        isPro: features.length > 0,
                        features: features,
                        updatedAt: new Date().toISOString(),
                    });
                    console.log(`Updated features for user ${uid}: ${features.join(', ')}`);
                }
                break;
            }

            case 'entitlements.active_entitlement_summary.updated': {
                const summary = event.data.object as any; // Active Entitlement Summary
                const customerId = summary.customer;

                const userSnapshot = await db.collection('users')
                    .where('stripeCustomerId', '==', customerId)
                    .limit(1)
                    .get();

                if (userSnapshot.empty) break;

                const userDoc = userSnapshot.docs[0];
                const entitlements = await stripe.entitlements.activeEntitlements.list({
                    customer: customerId,
                });
                const features = entitlements.data.map((e) => e.feature.lookup_key);

                await userDoc.ref.update({
                    isPro: features.length > 0,
                    features: features,
                    updatedAt: new Date().toISOString(),
                });
                console.log(`Synced entitlements for user ${userDoc.id}: ${features.join(', ')}`);
                break;
            }
        }

        return new Response(JSON.stringify({ received: true }), { status: 200 });
    } catch (error: any) {
        console.error(`Error processing webhook event: ${error.message}`);
        return new Response(`Webhook Error: ${error.message}`, { status: 500 });
    }
};

export const prerender = false;
