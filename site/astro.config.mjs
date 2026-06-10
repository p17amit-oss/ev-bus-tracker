import { defineConfig } from 'astro/config';

// Fully static output — Cloudflare Pages free tier serves it with no
// functions, no KV, no bill.
export default defineConfig({
  output: 'static',
  site: 'https://evbus-tracker.pages.dev', // swap for custom domain later
});
