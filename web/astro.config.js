// @ts-check
import { defineConfig } from 'astro/config';
import cloudflare from '@astrojs/cloudflare';

import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
    vite: {
        envDir: '..',
        ssr: {
            // Provided by the Workers runtime (nodejs_compat).
            external: ['node:buffer'],
        },
    },
    site: 'https://overlayr.app',
    // Astro 5: 'hybrid' is removed, use 'static'. 
    // Individual routes use 'export const prerender = false' for SSR.
    output: 'static',
    // Runs on Cloudflare Workers; bindings and secrets are in wrangler.jsonc.
    adapter: cloudflare({
        imageService: 'passthrough',
    }),
    compressHTML: true,

    build: {
        inlineStylesheets: 'auto',
    },

    i18n: {
        defaultLocale: 'en',
        locales: ['en', 'ja', 'es', 'th'],
        routing: {
            prefixDefaultLocale: false
        }
    },

    integrations: [sitemap()],

    server: {
        headers: {
            "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
        }
    }
});