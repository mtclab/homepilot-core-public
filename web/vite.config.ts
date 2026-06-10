import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		proxy: {
			'/artifacts': 'http://localhost:8000',
			'/inventory': 'http://localhost:8000',
			'/kb': 'http://localhost:8000',
			'/health': 'http://localhost:8000',
			'/auth': 'http://localhost:8000',
		},
	},
});
