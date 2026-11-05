// Desktop app releases, in the RELEASES R2 bucket. The release workflow
// (.github/workflows/release.yml) uploads each version's installers to
// `v<version>/` and then replaces `latest.json`, which points at them.
import { env } from 'cloudflare:workers';

// `linux` is the tarball, kept for older Linux installs that update from
// it; `appimage` is what the site offers Linux players.
export const PLATFORMS = ['windows', 'macos', 'linux', 'appimage'] as const;
export type Platform = (typeof PLATFORMS)[number];

export type ReleaseFile = {
    name: string;
    size: number;
    sha256: string;
};

export type Release = {
    version: string;
    date: string;
    notes: string[];
    files: Partial<Record<Platform, ReleaseFile>>;
};

export function isPlatform(value: string): value is Platform {
    return (PLATFORMS as readonly string[]).includes(value);
}

export async function latestRelease(): Promise<Release | null> {
    const object = await env.RELEASES.get('latest.json');
    return object ? object.json<Release>() : null;
}

/** The installer for `platform` from the latest release, or null. */
export async function latestDownload(platform: Platform) {
    const release = await latestRelease();
    // Releases from before the AppImage offer the tarball instead.
    const file = platform === 'appimage'
        ? (release?.files.appimage ?? release?.files.linux)
        : release?.files[platform];
    if (!release || !file) return null;
    const object = await env.RELEASES.get(`v${release.version}/${file.name}`);
    return object ? { file, object } : null;
}

/** The platform a browser's User-Agent most likely runs, or null. */
export function platformFromUserAgent(userAgent: string): Platform | null {
    // Phones and tablets can't run the desktop app.
    if (/Android|iPhone|iPad|iPod/i.test(userAgent)) return null;
    if (/Windows/i.test(userAgent)) return 'windows';
    if (/Macintosh|Mac OS X/i.test(userAgent)) return 'macos';
    if (/Linux|X11|CrOS/i.test(userAgent)) return 'appimage';
    return null;
}
