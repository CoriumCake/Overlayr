import type { APIRoute } from 'astro';
import type Stripe from 'stripe';
import { setPaymentStatus, setSessionStatus } from '../../lib/sales';
import { cryptoProvider, stripe } from '../../lib/stripe';

/**
 * Stripe events for pack purchases, from creators' accounts (a Connect
 * endpoint, since the charges are direct). Events can arrive more than once
 * and out of order; every handler sets a final state, so replays are harmless.
 * Subscribe the endpoint to: checkout.session.completed,
 * checkout.session.async_payment_succeeded, checkout.session.async_payment_failed,
 * checkout.session.expired, charge.refunded, charge.dispute.created and
 * charge.dispute.closed.
 */
export const POST: APIRoute = async ({ request }) => {
    const signature = request.headers.get('stripe-signature');
    const endpointSecret = process.env.STRIPE_WEBHOOK_SECRET;
    if (!signature || !endpointSecret) {
        return new Response('Webhook signature or secret missing', { status: 400 });
    }

    let event: Stripe.Event;
    try {
        event = await stripe().webhooks.constructEventAsync(
            await request.text(),
            signature,
            endpointSecret,
            undefined,
            cryptoProvider,
        );
    } catch (error: any) {
        console.error(`Webhook signature verification failed: ${error.message}`);
        return new Response('Invalid signature', { status: 400 });
    }

    try {
        switch (event.type) {
            case 'checkout.session.completed':
            case 'checkout.session.async_payment_succeeded': {
                const session = event.data.object;
                const paymentIntent = typeof session.payment_intent === 'string' ? session.payment_intent : null;
                // PromptPay and other delayed methods complete unpaid first,
                // then send async_payment_succeeded.
                if (session.payment_status === 'paid') {
                    await setSessionStatus(session.id, 'paid', paymentIntent);
                }
                break;
            }
            case 'checkout.session.async_payment_failed':
            case 'checkout.session.expired': {
                const session = event.data.object;
                await setSessionStatus(session.id, 'failed');
                break;
            }
            case 'charge.refunded': {
                const charge = event.data.object;
                // A partial refund leaves the pack with the buyer.
                if (charge.refunded && typeof charge.payment_intent === 'string') {
                    await setPaymentStatus(charge.payment_intent, 'refunded');
                }
                break;
            }
            case 'charge.dispute.created':
            case 'charge.dispute.closed': {
                const dispute = event.data.object;
                if (typeof dispute.payment_intent !== 'string') break;
                const won = event.type === 'charge.dispute.closed' && dispute.status === 'won';
                // Closed as lost: the buyer got their money back, so it stays disputed.
                if (event.type === 'charge.dispute.created' || won) {
                    await setPaymentStatus(dispute.payment_intent, won ? 'paid' : 'disputed');
                }
                break;
            }
        }
        return new Response(JSON.stringify({ received: true }), { status: 200 });
    } catch (error: any) {
        // Stripe retries on a 5xx.
        console.error(`Error processing webhook event ${event.id}: ${error.message}`);
        return new Response('Webhook handler failed', { status: 500 });
    }
};

export const prerender = false;
