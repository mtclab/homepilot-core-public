<script lang="ts">
	// One icon from the set in $lib/icons. Decorative by default: a visible
	// word always accompanies an icon, so the icon is hidden from assistive
	// tech. An icon-only control passes `label`, which makes the SVG an image
	// with that name (or give the control its own aria-label and leave this
	// decorative).
	import { ICONS, type IconName } from '$lib/icons';

	export let name: IconName;
	/** Pixel size of the square. 14 fits 12-13 px text on one baseline. */
	export let size = 14;
	/** Accessible name; when empty the icon is aria-hidden. */
	export let label = '';
</script>

<svg
	class="icon inline-block shrink-0 align-[-2px] {$$props.class ?? ''}"
	width={size}
	height={size}
	viewBox="0 0 16 16"
	fill="none"
	stroke="currentColor"
	stroke-width="1.5"
	stroke-linecap="round"
	stroke-linejoin="round"
	data-icon={name}
	role={label ? 'img' : undefined}
	aria-label={label || undefined}
	aria-hidden={label ? undefined : 'true'}
	focusable="false"
>
	{#each ICONS[name] as d}
		<path {d} />
	{/each}
</svg>
