import type { APIRoute } from 'astro';
import { Buffer } from 'node:buffer';
import { json, requireUser } from '../../../lib/auth';
import {
    MAX_COVER_BYTES,
    MAX_PACK_BYTES,
    allowHeavy,
    getListing,
    publicListing,
    saveCover,
    savePack,
    validId,
    type ListingRow,
} from '../../../lib/marketplace';
import { payableAccount, validPrice } from '../../../lib/sales';

function text(value: unknown, max: number) {
    return typeof value === 'string' ? value.trim().slice(0, max) : '';
}

/**
 * Publishes a pack, or a new version of one the caller already published.
 * Body: { pack: <pack JSON string>, title, description, cover: <base64 PNG>,
 * price: <satang, 0 for free> }. Selling needs a creator account that can
 * receive payouts.
 */
export const POST: APIRoute = async ({ request }) => {
    const user = await requireUser(request);
    if (!user) return json({ error: 'Log in to publish packs' }, 401);
    if (!(await allowHeavy(user.id))) return json({ error: 'Too many requests. Try again in a minute' }, 429);
    try {
        const body = await request.json();
        const packJson = typeof body.pack === 'string' ? body.pack : '';
        if (!packJson || Buffer.byteLength(packJson) > MAX_PACK_BYTES) {
            return json({ error: 'The pack is empty or larger than 3 MB' }, 413);
        }
        const pack = JSON.parse(packJson);
        const id = typeof pack.id === 'string' ? pack.id : '';
        if (!validId(id) || !Array.isArray(pack.entries)) {
            return json({ error: 'This is not a valid translation pack' }, 400);
        }
        const translated = pack.entries.filter(
            (entry: any) => typeof entry?.translation === 'string' && entry.translation.trim(),
        ).length;
        if (translated === 0) return json({ error: 'The pack has no translations yet' }, 400);
        const title = text(body.title, 80) || text(pack.name, 80);
        if (!title) return json({ error: 'Give the pack a title' }, 400);
        const coverBase64 = typeof body.cover === 'string' ? body.cover : '';
        const cover = Buffer.from(coverBase64, 'base64');
        const isPng = cover.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
        if (coverBase64 && (cover.length > MAX_COVER_BYTES || !isPng)) {
            return json({ error: 'The cover must be a PNG under 400 KB' }, 400);
        }

        const price = validPrice(body.price);
        if (price === null) return json({ error: 'The price must be free or between ฿10 and ฿10,000' }, 400);
        if (price > 0 && !(await payableAccount(user.id))) {
            return json({ error: 'Set up payouts on your Overlayr profile before selling packs', setup: 'payouts' }, 403);
        }

        const existing = await getListing(id);
        if (existing && existing.owner_id !== user.id) {
            return json({ error: 'Another creator already published this pack' }, 403);
        }
        const now = new Date().toISOString();
        const plain = Buffer.from(packJson, 'utf8');
        const version = (existing?.version ?? 0) + 1;
        const coverKey = coverBase64 ? await saveCover(id, version, cover) : existing?.cover_key ?? '';
        const row: ListingRow = {
            id,
            owner_id: user.id,
            title,
            description: text(body.description, 2000),
            application: text(pack.application, 120),
            source_lang: text(pack.source_lang, 16),
            target_lang: text(pack.target_lang, 16),
            entries: translated,
            author: text(user.name, 80) || text(pack.author, 80) || 'Anonymous',
            cover_key: coverKey,
            price,
            hidden: 0,
            version,
            downloads: existing?.downloads ?? 0,
            size: plain.length,
            created_at: existing?.created_at ?? now,
            updated_at: now,
        };
        await savePack(row, plain);
        return json({ listing: publicListing(row) });
    } catch (error) {
        console.error('Error publishing pack:', error);
        return json({ error: 'Could not publish the pack' }, 500);
    }
};

export const prerender = false;
