// @ts-check
import { defineConfig } from 'astro/config';
import vercel from '@astrojs/vercel';

import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
    vite: {
        envDir: '..',
    },
    site: 'https://overlayr.catcode.tech',
    // Astro 5: 'hybrid' is removed, use 'static'. 
    // Individual routes use 'export const prerender = false' for SSR.
    output: 'static',
    adapter: vercel({
        webAnalytics: {
            enabled: false,
        },
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