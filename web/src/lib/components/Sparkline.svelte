<script lang="ts">
	// A time series, hand-rolled in inline SVG (same approach as Donut.svelte —
	// no chart library ships with this UI). Colour comes from the categorical
	// chart ramp by default: a metric has no status meaning, and painting one
	// green would say "healthy" when it only says "memory".
	import { formatMetricValue, metricLabel, sparklineShape, type MetricPoint } from '$lib/sparkline';

	export let points: MetricPoint[] = [];
	export let metric = '';
	export let width = 132;
	export let height = 30;
	export let color = 'var(--chart-1)';
	export let showLabel = true;

	$: shape = sparklineShape(points, width, height);
	$: label = metricLabel(metric);
</script>

<div class="flex items-center gap-3">
	{#if showLabel}
		<span class="text-muted text-xs w-28 shrink-0">{label}</span>
	{/if}
	{#if shape}
		<svg
			{width}
			{height}
			viewBox="0 0 {width} {height}"
			class="shrink-0 overflow-visible"
			role="img"
			aria-label="{label}: {points.length} points, latest {formatMetricValue(metric, shape.last)}"
		>
			<path d={shape.area} fill={color} opacity="0.12" />
			<path d={shape.line} fill="none" stroke={color} stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
			<circle cx={shape.lastX} cy={shape.lastY} r="1.8" fill={color} />
		</svg>
		<span class="num text-ink text-xs tabular-nums">{formatMetricValue(metric, shape.last)}</span>
		<span class="text-muted text-[11px] tabular-nums">
			{formatMetricValue(metric, shape.min)} – {formatMetricValue(metric, shape.max)}
		</span>
	{:else}
		<span class="prose-note text-xs">No samples in this window.</span>
	{/if}
</div>
