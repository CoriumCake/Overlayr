// Stripe for Cloudflare Workers: requests go through fetch and webhook
// signatures are checked with WebCrypto. Created on first use, because the
// Worker's secrets are only in process.env while handling a request. Uses
// the API version the SDK was built for.
import Stripe from 'stripe';

let client: Stripe | null = null;

export function stripe(): Stripe {
    client ??= new Stripe(process.env.STRIPE_SECRET_KEY as string, {
        httpClient: Stripe.createFetchHttpClient(),
    });
    return client;
}

export const cryptoProvider = Stripe.createSubtleCryptoProvider();
