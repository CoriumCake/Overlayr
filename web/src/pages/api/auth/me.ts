import type { APIRoute } from 'astro';
import { json, requireUser } from '../../../lib/auth';

/** The logged-in user, or null. */
export const GET: APIRoute = async ({ request }) => json({ user: await requireUser(request) });

export const prerender = false;
