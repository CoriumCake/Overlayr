import type { APIRoute } from 'astro';
import { json } from '../../lib/auth';
import { LANGUAGES } from '../../lib/translate';

/** The languages /api/translate supports, in LibreTranslate's format. */
export const GET: APIRoute = () =>
    json(
        LANGUAGES.map((code) => ({ code, name: code, targets: LANGUAGES })),
        200,
        { 'Cache-Control': 'public, max-age=86400' },
    );

export const prerender = false;
