/** @type {import('tailwindcss').Config} */
export default {
	content: ['./src/**/*.{html,js,svelte,ts}'],
	theme: {
		extend: {
			colors: {
				surface: '#1e293b',
				border: '#334155',
			},
			fontFamily: {
				mono: ['JetBrains Mono', 'Fira Code', 'ui-monospace', 'monospace'],
			},
		},
	},
	plugins: [],
};
