// Selling packs with Stripe Connect.
//
// Each creator who sells has a Stripe connected account (Accounts v2,
// merchant configuration). A buyer pays on a Stripe-hosted Checkout page
// created on the creator's account (a direct charge), and Overlayr takes its
// commission as the application fee.
//
// This is what Stripe allows a Thai platform: Stripe, not Overlayr, is liable
// for the creators' accounts, so charges must be direct and the creator is the
// seller of record. Overlayr still pays Stripe's fees ("application" fee
// collector) out of its commission, so creators get exactly their share.
// Creators have no Stripe Dashboard: they set up and update their account on
// Stripe-hosted pages, and see payouts in Stripe's embedded components on
// their Overlayr profile.
import { env } from 'cloudflare:workers';
import type { User } from './auth';
import type { ListingRow } from './marketplace';
import { stripe } from './stripe';

/** Overlayr's share of each sale. */
export const COMMISSION = 0.35;
/** Price limits in satang: Stripe's minimum THB charge is about ฿10. */
export const MIN_PRICE = 1_000;
export const MAX_PRICE = 1_000_000;

export function commission(amount: number) {
    return Math.round(amount * COMMISSION);
}

/** A valid listing price in satang, or null. */
export function validPrice(value: unknown): number | null {
    const price = Number(value ?? 0);
    if (!Number.isInteger(price)) return null;
    if (price === 0) return 0;
    return price >= MIN_PRICE && price <= MAX_PRICE ? price : null;
}

// ---- Creator accounts ----

type CreatorRow = { stripe_account_id: string; payouts_enabled: number };

async function creatorRow(userId: string) {
    return env.DB.prepare('SELECT stripe_account_id, payouts_enabled FROM creator_accounts WHERE user_id = ?')
        .bind(userId)
        .first<CreatorRow>();
}

/** The creator's connected account, created on first use. */
export async function ensureCreatorAccount(user: User): Promise<string> {
    const existing = await creatorRow(user.id);
    if (existing) return existing.stripe_account_id;
    const account = await stripe().v2.core.accounts.create({
        contact_email: user.email ?? undefined,
        display_name: user.name ?? undefined,
        dashboard: 'none',
        // Prefilled so onboarding only asks the creator about themselves.
        identity: { country: 'th', entity_type: 'individual' },
        defaults: {
            currency: 'thb',
            responsibilities: { fees_collector: 'application', losses_collector: 'stripe' },
            profile: {
                business_url: 'https://overlayr.app',
                product_description: 'Game translation packs sold on the Overlayr marketplace',
            },
        },
        configuration: {
            merchant: {
                // Digital goods: books, movies, music and other media.
                mcc: '5815',
                capabilities: { card_payments: { requested: true }, promptpay_payments: { requested: true } },
            },
        },
        metadata: { overlayr_user_id: user.id },
    });
    // Two first requests racing: keep whichever account was saved first.
    await env.DB.prepare(
        'INSERT INTO creator_accounts (user_id, stripe_account_id) VALUES (?, ?) ON CONFLICT (user_id) DO NOTHING',
    )
        .bind(user.id, account.id)
        .run();
    return (await creatorRow(user.id))!.stripe_account_id;
}

/** Asks Stripe whether the account can take card payments, and saves it. */
async function refreshPayouts(userId: string, accountId: string) {
    const account = await stripe().v2.core.accounts.retrieve(accountId, { include: ['configuration.merchant'] });
    const status = account.configuration?.merchant?.capabilities?.card_payments?.status;
    const enabled = status === 'active';
    await env.DB.prepare('UPDATE creator_accounts SET payouts_enabled = ?, updated_at = ? WHERE user_id = ?')
        .bind(enabled ? 1 : 0, new Date().toISOString(), userId)
        .run();
    return enabled;
}

/**
 * The creator's account if it can sell. Checks with Stripe while not yet
 * enabled, so finishing onboarding takes effect without a webhook.
 */
export async function payableAccount(userId: string): Promise<string | null> {
    const row = await creatorRow(userId);
    if (!row) return null;
    if (row.payouts_enabled) return row.stripe_account_id;
    return (await refreshPayouts(userId, row.stripe_account_id)) ? row.stripe_account_id : null;
}

/** What the profile page shows a creator. */
export async function creatorStatus(userId: string) {
    const row = await creatorRow(userId);
    const payoutsEnabled = row ? Boolean(await payableAccount(userId)) : false;
    const sales = await env.DB.prepare(
        `SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS gross, COALESCE(SUM(amount - fee), 0) AS earned
         FROM marketplace_purchases WHERE creator_id = ? AND status = 'paid'`,
    )
        .bind(userId)
        .first<{ count: number; gross: number; earned: number }>();
    return {
        connected: Boolean(row),
        payoutsEnabled,
        commission: COMMISSION,
        sales: sales?.count ?? 0,
        gross: sales?.gross ?? 0,
        earned: sales?.earned ?? 0,
    };
}

/**
 * A Stripe-hosted page where the creator sets up their account, or updates
 * it once set up, and then comes back to their profile. Used instead of the
 * embedded onboarding component, whose Stripe sign-in step opens a pop-up.
 */
export async function accountLink(accountId: string, origin: string, update: boolean) {
    const urls = {
        configurations: ['merchant' as const],
        refresh_url: `${origin}/profile?setup=retry`,
        return_url: `${origin}/profile?setup=done`,
    };
    const link = await stripe().v2.core.accountLinks.create({
        account: accountId,
        use_case: update
            ? { type: 'account_update', account_update: urls }
            : { type: 'account_onboarding', account_onboarding: urls },
    });
    return link.url;
}

/** A client secret for the embedded payout and notice components. */
export async function accountSession(accountId: string) {
    const session = await stripe().accountSessions.create({
        account: accountId,
        components: {
            notification_banner: { enabled: true },
            payouts: { enabled: true },
        },
    });
    return session.client_secret;
}

// ---- Purchases ----

/** Whether the user paid for the pack and still owns it. */
export async function hasPaid(userId: string, packId: string) {
    const row = await env.DB.prepare(
        "SELECT 1 AS paid FROM marketplace_purchases WHERE user_id = ? AND pack_id = ? AND status = 'paid' LIMIT 1",
    )
        .bind(userId, packId)
        .first();
    return Boolean(row);
}

/** IDs of the packs the user bought. */
export async function ownedPacks(userId: string): Promise<string[]> {
    const { results } = await env.DB.prepare(
        "SELECT DISTINCT pack_id FROM marketplace_purchases WHERE user_id = ? AND status = 'paid'",
    )
        .bind(userId)
        .all<{ pack_id: string }>();
    return results.map((row) => row.pack_id);
}

export class NotForSale extends Error {}

/** Starts paying for a pack; returns the Checkout page to send the buyer to. */
export async function startCheckout(user: User, listing: ListingRow, origin: string) {
    if (!listing.price || listing.hidden) throw new NotForSale('This pack is not for sale');
    if (listing.owner_id === user.id) throw new NotForSale('This is your own pack');
    if (await hasPaid(user.id, listing.id)) throw new NotForSale('You already own this pack');
    const account = await payableAccount(listing.owner_id);
    if (!account) throw new NotForSale('This pack cannot be bought right now');

    const fee = commission(listing.price);
    const back = `${origin}/buy?pack=${encodeURIComponent(listing.id)}`;
    const session = await stripe().checkout.sessions.create(
        {
            mode: 'payment',
            payment_method_types: ['card', 'promptpay'],
            line_items: [
                {
                    quantity: 1,
                    price_data: {
                        currency: 'thb',
                        unit_amount: listing.price,
                        product_data: {
                            name: listing.title,
                            description: `Overlayr translation pack by ${listing.author}`.slice(0, 200),
                        },
                    },
                },
            ],
            payment_intent_data: {
                application_fee_amount: fee,
                metadata: { pack_id: listing.id, user_id: user.id },
            },
            client_reference_id: user.id,
            customer_email: user.email ?? undefined,
            metadata: { pack_id: listing.id, user_id: user.id },
            success_url: `${back}&session={CHECKOUT_SESSION_ID}`,
            cancel_url: `${back}&cancelled=1`,
        },
        { stripeAccount: account },
    );
    // The amounts come from here, not from the webhook, so they are what the
    // buyer was actually shown.
    await env.DB.prepare(
        `INSERT INTO marketplace_purchases (checkout_session_id, user_id, pack_id, creator_id, amount, fee, status)
         VALUES (?, ?, ?, ?, ?, ?, 'pending')`,
    )
        .bind(session.id, user.id, listing.id, listing.owner_id, listing.price, fee)
        .run();
    return session.url!;
}

type PurchaseStatus = 'paid' | 'failed' | 'refunded' | 'disputed';

/** Records a payment outcome for a Checkout Session; returns the purchase. */
export async function setSessionStatus(sessionId: string, status: PurchaseStatus, paymentIntentId?: string | null) {
    return env.DB.prepare(
        `UPDATE marketplace_purchases
         SET status = ?, payment_intent_id = COALESCE(?, payment_intent_id), updated_at = ?
         WHERE checkout_session_id = ?
         RETURNING user_id, pack_id`,
    )
        .bind(status, paymentIntentId ?? null, new Date().toISOString(), sessionId)
        .first<{ user_id: string; pack_id: string }>();
}

/**
 * Records a refund or dispute outcome for a payment. A refunded or disputed
 * pack stops unlocking on the buyer's next start.
 */
export async function setPaymentStatus(paymentIntentId: string, status: PurchaseStatus) {
    const purchase = await env.DB.prepare(
        `UPDATE marketplace_purchases SET status = ?, updated_at = ? WHERE payment_intent_id = ?
         RETURNING user_id, pack_id`,
    )
        .bind(status, new Date().toISOString(), paymentIntentId)
        .first<{ user_id: string; pack_id: string }>();
    if (purchase && status !== 'paid') {
        await env.DB.prepare('UPDATE marketplace_licenses SET revoked = 1 WHERE user_id = ? AND pack_id = ?')
            .bind(purchase.user_id, purchase.pack_id)
            .run();
    }
    return purchase;
}

/** Whether any buyer paid for the pack, so removing it must not delete it. */
export async function hasSales(packId: string) {
    const row = await env.DB.prepare(
        "SELECT 1 AS sold FROM marketplace_purchases WHERE pack_id = ? AND status = 'paid' LIMIT 1",
    )
        .bind(packId)
        .first();
    return Boolean(row);
}
