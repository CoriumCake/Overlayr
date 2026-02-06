// @ts-check
import { defineConfig } from 'astro/config';

import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
    site: 'https://overlayr.catcode.tech',
    output: 'static',
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