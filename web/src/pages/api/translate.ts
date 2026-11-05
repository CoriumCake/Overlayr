import type { APIRoute } from 'astro';
import { json } from '../../lib/auth';
import { MAX_TEXT_LENGTH, languageName, translate } from '../../lib/translate';

/**
 * Translates on-screen text for the desktop app, free and without an
 * account. LibreTranslate-compatible: `{ q, source, target }` in,
 * `{ translatedText }` out, one text per request so the rate limit bounds
 * cost. Each IP address is limited by TRANSLATE_LIMIT (see wrangler.jsonc).
 */
export const POST: APIRoute = async ({ request }) => {
    const { env } = await import('cloudflare:workers');
    let body: { q?: unknown; source?: unknown; target?: unknown };
    try {
        body = await request.json();
    } catch {
        return json({ error: 'The request body must be JSON' }, 400);
    }
    const source = languageName(body.source);
    const target = languageName(body.target);
    if (!source || !target) {
        return json({ error: 'Unknown language code; use codes such as "ja" or "zh-TW"' }, 400);
    }
    const text = body.q;
    if (typeof text !== 'string' || text.length > MAX_TEXT_LENGTH) {
        return json({ error: `q must be text of at most ${MAX_TEXT_LENGTH} characters` }, 400);
    }

    const ip = request.headers.get('cf-connecting-ip');
    if (env.TRANSLATE_LIMIT && ip) {
        const { success } = await env.TRANSLATE_LIMIT.limit({ key: ip });
        if (!success) {
            return json({ error: 'Too many translations. Try again in a minute' }, 429, { 'Retry-After': '60' });
        }
    }
    if (!env.AI) {
        return json({ error: 'Translation is not available right now' }, 503);
    }

    try {
        return json({ translatedText: await translate(env.AI, source, target, text) });
    } catch (error) {
        console.error('Translation failed:', error);
        return json({ error: 'Translation failed. Try again shortly' }, 502);
    }
};

/** Health check: the desktop app treats any answer as "reachable". */
export const GET: APIRoute = () => json({ error: 'Use POST' }, 405, { Allow: 'POST' });

export const prerender = false;
