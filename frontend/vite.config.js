import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';
export default defineConfig({
    plugins: [tailwindcss(), react()],
    resolve: {
        alias: {
            '@': path.resolve(__dirname, './src'),
        },
    },
    server: {
        host: '127.0.0.1',
        port: 5173,
        strictPort: false,
        proxy: {
            '/api': {
                target: 'http://127.0.0.1:47821',
                changeOrigin: true,
            },
            '/ws': {
                target: 'http://127.0.0.1:47821',
                ws: true,
                changeOrigin: true,
            },
        },
    },
    build: {
        outDir: 'dist',
        sourcemap: true,
    },
});
