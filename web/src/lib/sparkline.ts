// Sparkline geometry. Kept out of the component so the maths is unit-tested
// rather than eyeballed in a browser, and so nothing here needs a chart library.

export interface MetricPoint {
	ts: number;
	value: number;
}

export interface SparklineShape {
	// Polyline path for the series.
	line: string;
	// The same series closed to the baseline, for a faint fill under the line.
	area: string;
	min: number;
	max: number;
	first: number;
	last: number;
	// Position of the newest point, for the end dot.
	lastX: number;
	lastY: number;
}

function round(n: number): number {
	return Math.round(n * 100) / 100;
}

// Builds the geometry for `points` inside a `width` x `height` box.
//
// The y-scale spans the data's own min..max, never a forced zero: a sparkline
// answers "what shape is this" and a load average that wanders between 0.4 and
// 0.6 must not render as a flat line at the bottom of a 0..1 box. A series with
// no variation is drawn on the vertical centre rather than dividing by zero.
//
// Returns null for an empty series so the caller renders an empty state instead
// of an axis with nothing on it.
export function sparklineShape(
	points: MetricPoint[],
	width = 120,
	height = 28,
	pad = 2,
): SparklineShape | null {
	if (!points.length) return null;

	const values = points.map((p) => p.value);
	const min = Math.min(...values);
	const max = Math.max(...values);
	const span = max - min;
	const innerH = Math.max(1, height - pad * 2);
	const innerW = Math.max(1, width - pad * 2);

	const x = (i: number) => round(pad + (points.length === 1 ? innerW : (i / (points.length - 1)) * innerW));
	// SVG y grows downward, so the largest value sits at the smallest y.
	const y = (v: number) => round(span === 0 ? pad + innerH / 2 : pad + (1 - (v - min) / span) * innerH);

	const coords = points.map((p, i) => [x(i), y(p.value)] as const);
	const line = coords.map(([px, py], i) => `${i === 0 ? 'M' : 'L'}${px} ${py}`).join(' ');
	const first = coords[0];
	const lastPoint = coords[coords.length - 1];
	const baseline = round(height - pad / 2);
	const area = `${line} L${lastPoint[0]} ${baseline} L${first[0]} ${baseline} Z`;

	return {
		line,
		area,
		min,
		max,
		first: values[0],
		last: values[values.length - 1],
		lastX: lastPoint[0],
		lastY: lastPoint[1],
	};
}

// Metric names are storage keys (`disk.free_gb`); this is how they read in the UI.
const METRIC_LABELS: Record<string, string> = {
	'cpu.count': 'CPU cores',
	'disk.total_gb': 'Disk total',
	'disk.free_gb': 'Disk free',
	'memory.total_gb': 'Memory total',
	'memory.free_gb': 'Memory free',
	'load.1m': 'Load 1m',
	'load.5m': 'Load 5m',
	'load.15m': 'Load 15m',
};

export function metricLabel(metric: string): string {
	return METRIC_LABELS[metric] ?? metric;
}

// The unit is carried by the metric NAME (the agent reports GB and unitless
// counts), so it is read off the suffix rather than guessed from the value.
export function metricUnit(metric: string): string {
	if (metric.endsWith('_gb')) return ' GB';
	if (metric.endsWith('_pct')) return '%';
	return '';
}

export function formatMetricValue(metric: string, value: number): string {
	const unit = metricUnit(metric);
	const digits = Number.isInteger(value) ? 0 : 2;
	return `${value.toFixed(digits)}${unit}`;
}
