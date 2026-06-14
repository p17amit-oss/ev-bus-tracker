import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  output: 'static',
  site: 'https://ev-bus-tracker.pages.dev', // swap for custom domain — update here when ready
  integrations: [sitemap()],
});
