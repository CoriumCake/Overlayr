-- Paid marketplace packs with Stripe Connect, replacing subscriptions.
-- Buyers pay Overlayr; Stripe sends the creator's share to their connected
-- account in the same payment (a destination charge).

-- Subscriptions are gone (never in production, so nothing is lost).
DROP TABLE IF EXISTS profiles;

-- Price in satang (1/100 baht); 0 is free.
ALTER TABLE marketplace_listings ADD COLUMN price INTEGER NOT NULL DEFAULT 0;
-- A removed pack that people paid for: no longer listed, but its buyers can
-- still install and use it.
ALTER TABLE marketplace_listings ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;

-- A creator's Stripe connected account, which receives their share of sales.
CREATE TABLE creator_accounts (
    user_id TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    stripe_account_id TEXT NOT NULL UNIQUE,
    -- Whether Stripe has verified the account to receive transfers; checked
    -- again with Stripe before it changes from 0 to 1.
    payouts_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- One row per Checkout Session for a paid pack, written by the webhook.
-- Only 'paid' purchases unlock the pack; refunds and disputes take it back.
-- No foreign key to the listing, so sales records outlive unpublished packs.
CREATE TABLE marketplace_purchases (
    checkout_session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    pack_id TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    -- Satang: what the buyer paid, and Overlayr's commission from it.
    amount INTEGER NOT NULL,
    fee INTEGER NOT NULL,
    payment_intent_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'failed', 'refunded', 'disputed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX marketplace_purchases_owner ON marketplace_purchases (user_id, pack_id);
CREATE INDEX marketplace_purchases_payment ON marketplace_purchases (payment_intent_id);
