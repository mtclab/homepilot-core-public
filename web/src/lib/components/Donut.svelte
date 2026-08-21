<script lang="ts">
	// Current-state donut: renders segments as arcs of a single ring with a big
	// total in the centre. Time series are Sparkline.svelte's job.
	export let segments: { label: string; value: number; color: string }[] = [];
	export let centerLabel = '';
	export let centerSub = '';
	export let size = 132;
	export let thickness = 14;

	$: total = segments.reduce((s, x) => s + x.value, 0);
	$: r = (size - thickness) / 2;
	$: circ = 2 * Math.PI * r;
	$: arcs = (() => {
		let offset = 0;
		return segments
			.filter((s) => s.value > 0)
			.map((s) => {
				const frac = total ? s.value / total : 0;
				const seg = { ...s, dash: frac * circ, offset };
				offset += frac * circ;
				return seg;
			});
	})();
</script>

<div class="flex items-center gap-4">
	<svg width={size} height={size} viewBox="0 0 {size} {size}" class="shrink-0 -rotate-90">
		<circle cx={size / 2} cy={size / 2} {r} fill="none" stroke="var(--color-surface-raised)" stroke-width={thickness} />
		{#each arcs as a}
			<circle
				cx={size / 2}
				cy={size / 2}
				{r}
				fill="none"
				stroke={a.color}
				stroke-width={thickness}
				stroke-dasharray="{a.dash} {circ - a.dash}"
				stroke-dashoffset={-a.offset}
			/>
		{/each}
		<text x="50%" y="48%" text-anchor="middle" dominant-baseline="middle"
			transform="rotate(90 {size / 2} {size / 2})" fill="var(--color-text-strong)" font-size="22" font-weight="700"
			style="font-variant-numeric: tabular-nums">
			{centerLabel}
		</text>
		{#if centerSub}
			<text x="50%" y="62%" text-anchor="middle" dominant-baseline="middle"
				transform="rotate(90 {size / 2} {size / 2})" fill="var(--color-muted)" font-size="10">
				{centerSub}
			</text>
		{/if}
	</svg>
	<ul class="space-y-1 text-xs">
		{#each segments as s}
			<li class="flex items-center gap-2 text-ink">
				<span class="w-2.5 h-2.5 rounded-sm" style="background:{s.color}"></span>
				<span class="text-muted">{s.label}</span>
				<span class="ml-auto num text-ink">{s.value}</span>
			</li>
		{/each}
	</ul>
</div>
