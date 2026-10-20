import { defineMiddleware } from 'astro:middleware';

/** Limits each IP address's API requests (see API_LIMIT in wrangler.jsonc). */
export const onRequest = defineMiddleware(async (context, next) => {
    if (!context.url.pathname.startsWith('/api/') || context.isPrerendered) return next();
    const { env } = await import('cloudflare:workers');
    const ip = context.request.headers.get('cf-connecting-ip');
    if (env.API_LIMIT && ip) {
        const { success } = await env.API_LIMIT.limit({ key: ip });
        if (!success) {
            return new Response(JSON.stringify({ error: 'Too many requests. Try again in a minute' }), {
                status: 429,
                headers: { 'Content-Type': 'application/json', 'Retry-After': '60' },
            });
        }
    }
    return next();
});
