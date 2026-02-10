import type { APIRoute } from 'astro';
import Stripe from 'stripe';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY as string, {
    apiVersion: '2025-01-27.acacia' as any,
});

export const POST: APIRoute = async ({ request }) => {
    try {
        const { customerId } = await request.json();

        if (!customerId) {
            return new Response('Missing customerId', { status: 400 });
        }

        const session = await stripe.billingPortal.sessions.create({
            customer: customerId,
            return_url: `${new URL(request.url).origin}/profile`,
        });

        return new Response(JSON.stringify({ url: session.url }), { status: 200 });
    } catch (error: any) {
        console.error('Error creating portal session:', error);
        return new Response('Internal Server Error', { status: 500 });
    }
};

export const prerender = false;
