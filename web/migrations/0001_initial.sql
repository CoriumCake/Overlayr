-- Overlayr: accounts, subscriptions and the translation pack marketplace, in
-- Cloudflare D1. Only the Worker reaches this database, so access control is
-- in the API routes. Pack contents and covers live in R2.

-- One row per Google account that has logged in.
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    google_sub TEXT NOT NULL UNIQUE,
    email TEXT,
    name TEXT,
    avatar_url TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Logins: a browser (cookie) or the desktop app (refresh token). Only a
-- SHA-256 hash of the token is stored; desktop tokens are replaced on each
-- refresh.
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('web', 'desktop')),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX sessions_user ON sessions (user_id);

-- Subscription state, written by the Stripe webhook.
CREATE TABLE profiles (
    user_id TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    stripe_customer_id TEXT UNIQUE,
    subscription_id TEXT,
    is_pro INTEGER NOT NULL DEFAULT 0,
    plan TEXT,
    features TEXT NOT NULL DEFAULT '[]',
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    cancel_at TEXT,
    expiry_date TEXT,
    trial_start_date TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Published packs, as shown in the marketplace grid.
CREATE TABLE marketplace_listings (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    application TEXT NOT NULL DEFAULT '',
    source_lang TEXT NOT NULL DEFAULT '',
    target_lang TEXT NOT NULL DEFAULT '',
    entries INTEGER NOT NULL DEFAULT 0,
    author TEXT NOT NULL DEFAULT '',
    -- R2 key of the cover picture, empty without one.
    cover_key TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    downloads INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX marketplace_listings_owner ON marketplace_listings (owner_id);
CREATE INDEX marketplace_listings_updated ON marketplace_listings (updated_at DESC);

-- Each pack's master key (base64). Never sent to any client.
CREATE TABLE marketplace_keys (
    pack_id TEXT PRIMARY KEY REFERENCES marketplace_listings (id) ON DELETE CASCADE,
    key TEXT NOT NULL
);

-- One licence per user and pack: the key that user's copy was encrypted
-- with, so a leaked copy can be traced to its account.
CREATE TABLE marketplace_licenses (
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    pack_id TEXT NOT NULL REFERENCES marketplace_listings (id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    key TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    issued_at TEXT NOT NULL,
    PRIMARY KEY (user_id, pack_id)
);
CREATE INDEX marketplace_licenses_pack ON marketplace_licenses (pack_id);
-- The daily install limit counts a user's recent licences.
CREATE INDEX marketplace_licenses_recent ON marketplace_licenses (user_id, issued_at);
