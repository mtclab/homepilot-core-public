// The icon set (UI plan 4.8): inline SVG on a 16 px grid, 1.5 px stroke,
// `currentColor`, drawn for HomePilot. They replace the text glyphs and
// arrows that stood in for icons (review F8): those rendered in whatever font
// the platform had, at whatever size and weight it chose, and they are the
// "emoji as icon" tell the identity system names, one step removed.
//
// Each icon is a list of SVG path `d` strings on viewBox 0 0 16 16, stroked,
// never filled. A name used in markup that is not here fails svelte-check.

export const ICONS = {
	refresh: ['M13.25 8a5.25 5.25 0 1 1-1.54-3.71', 'M13.25 2.5v3h-3'],
	close: ['M4 4l8 8', 'M12 4l-8 8'],
	menu: ['M2.5 4h11', 'M2.5 8h11', 'M2.5 12h11'],
	check: ['M3 8.5l3 3 7-7'],
	'chevron-right': ['M6 3.5L10.5 8 6 12.5'],
	'chevron-left': ['M10 3.5L5.5 8 10 12.5'],
	'chevron-down': ['M3.5 6L8 10.5 12.5 6'],
	'arrow-right': ['M2.5 8h11', 'M9.5 4l4 4-4 4'],
	'arrow-left': ['M13.5 8h-11', 'M6.5 4l-4 4 4 4'],
	'external-link': [
		'M9.5 2.5h4v4',
		'M13.5 2.5l-6 6',
		'M12 9.5v3.5a.5.5 0 0 1-.5.5h-8.5a.5.5 0 0 1-.5-.5v-8.5a.5.5 0 0 1 .5-.5H7',
	],
	warning: ['M8 2.25l6.25 11h-12.5z', 'M8 6.5v3', 'M8 11.5v.25'],
	circle: ['M8 3.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 1 1 0-9z'],
	plus: ['M8 3v10', 'M3 8h10'],
} as const;

export type IconName = keyof typeof ICONS;
