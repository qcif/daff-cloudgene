import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// base must match the nginx alias (../nginx-uploads.conf) or every built
// asset URL resolves against '/' and 404s through the Cloudgene proxy.
export default defineConfig({
  base: '/uploads/',
  plugins: [vue()],
  server: {
    // Pinned: without strictPort Vite silently moves to the next free port
    // when 5173 is taken, and .vscode/launch.json opens a browser at 5173
    // regardless. Failing loudly beats debugging a blank tab.
    port: 5173,
    strictPort: true,
    proxy: {
      '/uploads/api': {
        target: 'http://127.0.0.1:8003',
        rewrite: (path) => path.replace(/^\/uploads\/api/, ''),
      },
    },
  },
  test: {
    environment: 'jsdom',
  },
});
