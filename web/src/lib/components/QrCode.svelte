<script lang="ts">
	// A QR code drawn in the browser (#822): the ntfy app's subscribe link,
	// scanned from the screen. Nothing leaves the page - a hosted QR service
	// would be handed the link. `uqr` (MIT, no dependencies, pinned in
	// package.json) only computes the module matrix; this draws it as one SVG
	// path. Always dark on white, whatever the theme: phone cameras read a
	// light-on-dark code badly.
	import { encode } from 'uqr';

	export let value: string;
	export let label: string;

	$: qr = encode(value, { ecc: 'M', border: 2 });
	$: path = qr.data
		.flatMap((row, y) => row.map((dark, x) => (dark ? `M${x} ${y}h1v1h-1z` : '')))
		.join('');
</script>

<svg
	class="qr"
	viewBox="0 0 {qr.size} {qr.size}"
	role="img"
	aria-label={label}
	shape-rendering="crispEdges"
	data-qr={value}
>
	<rect width={qr.size} height={qr.size} fill="#fff" />
	<path d={path} fill="#000" />
</svg>

<style>
	.qr {
		width: 12rem;
		height: 12rem;
		display: block;
	}
</style>
