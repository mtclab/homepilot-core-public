import { describe, expect, it } from 'vitest';
import {
	bucketExtremes,
	bucketedSeriesNote,
	formatMetricValue,
	metricLabel,
	metricUnit,
	sparklineShape,
} from './sparkline';

describe('sparklineShape', () => {
	it('returns null for an empty series so the caller can show an empty state', () => {
		expect(sparklineShape([])).toBeNull();
	});

	it('draws a single point without dividing by a zero span', () => {
		const shape = sparklineShape([{ ts: 1, value: 5 }], 100, 20);
		expect(shape).not.toBeNull();
		expect(shape!.line).toMatch(/^M/);
		expect(shape!.line).not.toContain('NaN');
		expect(shape!.min).toBe(5);
		expect(shape!.max).toBe(5);
	});

	it('draws a flat series on the vertical centre rather than producing NaN', () => {
		const points = [1, 2, 3].map((ts) => ({ ts, value: 0.4 }));
		const shape = sparklineShape(points, 100, 20)!;
		expect(shape.line).not.toContain('NaN');
		// span === 0 -> every y is the mid-line, so all y coordinates match.
		const ys = [...shape.line.matchAll(/[ML]([\d.]+) ([\d.]+)/g)].map((m) => m[2]);
		expect(new Set(ys).size).toBe(1);
	});

	it('scales to the data range, not to zero', () => {
		// A load average wandering 0.40..0.60 must use the full box height, or the
		// sparkline says "flat" about data that is not.
		const points = [
			{ ts: 1, value: 0.4 },
			{ ts: 2, value: 0.5 },
			{ ts: 3, value: 0.6 }
		];
		const shape = sparklineShape(points, 100, 20, 2)!;
		const ys = [...shape.line.matchAll(/[ML]([\d.]+) ([\d.]+)/g)].map((m) => Number(m[2]));
		// SVG y grows downward: the highest value sits at the smallest y.
		expect(ys[0]).toBeGreaterThan(ys[2]);
		expect(Math.max(...ys) - Math.min(...ys)).toBeGreaterThan(10);
	});

	it('spans the full width and ends on the newest point', () => {
		const points = [1, 2, 3, 4].map((ts) => ({ ts, value: ts }));
		const shape = sparklineShape(points, 120, 30, 2)!;
		const xs = [...shape.line.matchAll(/[ML]([\d.]+) ([\d.]+)/g)].map((m) => Number(m[1]));
		expect(xs[0]).toBe(2);
		expect(xs[xs.length - 1]).toBe(118);
		expect(shape.lastX).toBe(118);
		expect(shape.last).toBe(4);
		expect(shape.first).toBe(1);
	});

	it('closes the area path back to the baseline', () => {
		const shape = sparklineShape([
			{ ts: 1, value: 1 },
			{ ts: 2, value: 2 }
		])!;
		expect(shape.area.endsWith('Z')).toBe(true);
		expect(shape.area.startsWith(shape.line)).toBe(true);
	});
});

describe('metric naming', () => {
	it('labels the metrics the agent actually reports', () => {
		expect(metricLabel('disk.free_gb')).toBe('Disk free');
		expect(metricLabel('load.1m')).toBe('Load 1m');
	});

	it('labels the ratios the agent reports since #661, with a percent unit', () => {
		expect(metricLabel('disk.used_pct')).toBe('Disk used');
		expect(metricLabel('memory.used_pct')).toBe('Memory used');
		expect(metricLabel('load.1m_per_core')).toBe('Load 1m per core');
		expect(formatMetricValue('disk.used_pct', 91.25)).toBe('91.25%');
		expect(formatMetricValue('load.1m_per_core', 1.5)).toBe('1.50');
	});

	it('falls back to the raw key for a metric it does not know', () => {
		expect(metricLabel('something.new')).toBe('something.new');
	});

	it('reads the unit off the metric name, never off the value', () => {
		expect(metricUnit('memory.free_gb')).toBe(' GB');
		expect(metricUnit('cpu.count')).toBe('');
		expect(formatMetricValue('disk.free_gb', 12.345)).toBe('12.35 GB');
		expect(formatMetricValue('cpu.count', 8)).toBe('8');
	});
});

describe('a bucketed series says what it is (#661)', () => {
	it('names the bucket width and the aggregation', () => {
		expect(bucketedSeriesNote(600)).toBe(
			"Showing per-bucket min/avg/max over 10-minute buckets: the line is each bucket's average, the range is the lowest and highest sample in the window."
		);
		expect(bucketedSeriesNote(7200)).toContain('2-hour buckets');
	});

	it('says nothing about a raw series', () => {
		expect(bucketedSeriesNote(null)).toBe('');
		expect(bucketedSeriesNote(undefined)).toBe('');
	});

	it('takes the range from the bucket extremes, not from the averages', () => {
		const points = [
			{ ts: 0, value: 4.5, min: 0, max: 9 },
			{ ts: 600, value: 5, min: 2, max: 30 },
		];
		expect(bucketExtremes(points)).toEqual({ min: 0, max: 30 });
		expect(bucketExtremes([{ ts: 0, value: 1 }])).toBeNull();
		expect(bucketExtremes([])).toBeNull();
	});
});
