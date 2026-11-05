import type { APIRoute } from 'astro';
import { json } from '../../../lib/auth';
import { latestRelease } from '../../../lib/releases';

/** The latest desktop release: version, notes and installers. */
export const GET: APIRoute = async () => {
    try {
        const release = await latestRelease();
        return json({ release }, 200, { 'Cache-Control': 'public, max-age=60' });
    } catch (error) {
        console.error('Error reading the latest release:', error);
        return json({ error: 'Could not load the latest release' }, 500);
    }
};

export const prerender = false;
