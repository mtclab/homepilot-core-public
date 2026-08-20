/** @type {import('tailwindcss').Config} */
// The theme is a thin naming layer over the design tokens in src/app.css.
// Utilities in markup therefore say what a colour MEANS (bg-surface, text-muted,
// text-accent) and never which palette swatch it happens to be — swapping a
// token in app.css re-themes the whole UI, and the contrast gate
// (src/lib/tokens.test.ts) keeps that swap honest.
export default {
	content: ['./src/**/*.{html,js,svelte,ts}'],
	theme: {
		extend: {
			colors: {
				canvas: 'var(--color-bg)',
				surface: 'var(--color-surface)',
				raised: 'var(--color-surface-raised)',
				divider: 'var(--color-divider)',
				border: 'var(--color-border)',
				'border-strong': 'var(--color-border-strong)',

				ink: 'var(--color-text)',
				'ink-strong': 'var(--color-text-strong)',
				muted: 'var(--color-muted)',

				accent: 'var(--color-accent)',
				'accent-strong': 'var(--color-accent-strong)',
				'accent-tint': 'var(--color-accent-tint)',
				'accent-border': 'var(--color-accent-border)',

				ok: 'var(--color-ok)',
				'ok-tint': 'var(--color-ok-tint)',
				'ok-border': 'var(--color-ok-border)',
				warn: 'var(--color-warn)',
				'warn-tint': 'var(--color-warn-tint)',
				'warn-border': 'var(--color-warn-border)',
				danger: 'var(--color-danger)',
				'danger-tint': 'var(--color-danger-tint)',
				'danger-border': 'var(--color-danger-border)',
				info: 'var(--color-info)',
				'info-tint': 'var(--color-info-tint)',
				'info-border': 'var(--color-info-border)',
				note: 'var(--color-note)',
				'note-tint': 'var(--color-note-tint)',
				'note-border': 'var(--color-note-border)',
			},
			fontFamily: {
				serif: 'var(--font-serif)',
				sans: 'var(--font-sans)',
				mono: 'var(--font-mono)',
			},
			borderRadius: {
				sm: 'var(--radius-sm)',
				DEFAULT: 'var(--radius-sm)',
				md: 'var(--radius-md)',
				lg: 'var(--radius-md)',
			},
			spacing: {
				's-1': 'var(--space-1)',
				's-2': 'var(--space-2)',
				's-3': 'var(--space-3)',
				's-4': 'var(--space-4)',
				's-5': 'var(--space-5)',
				's-6': 'var(--space-6)',
			},
		},
	},
	plugins: [],
};
