import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	kit: {
		adapter: adapter({
			pages: 'dist',
			assets: 'dist',
			fallback: 'index.html',
			precompress: false,
		}),
		paths: {
			base: '/ui',
		},
		csp: {
			mode: 'auto',
			directives: {
				'default-src': ['self'],
				'script-src': ['self'],
				'connect-src': ['self', 'https:', 'http://localhost', 'http://127.0.0.1'],
				'img-src': ['self', 'data:'],
				'style-src': ['self', 'unsafe-inline'],
				'object-src': ['none'],
				'frame-ancestors': ['none'],
			},
		},
	},
};

export default config;
