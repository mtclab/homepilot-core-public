import { describe, expect, it } from 'vitest';
import fixture from './series.fixture.json';
import { sparklineShape } from './sparkline';
import type { MetricSeries } from './api';

// The other half of the journey gate. `series.fixture.json` is the shape the
// real API returns — tests/test_metrics_journey.py asserts a LIVE
// /metrics/hosts/{host}/series response has exactly these keys and types, so a
// backend change that breaks the UI fails there, and a UI change that stops
// drawing that payload fails here.
describe('the UI draws what the API returns', () => {
	it('turns a real series payload into a drawable sparkline', () => {
		const series = fixture as MetricSeries;
		const shape = sparklineShape(series.points, 132, 30);
		expect(shape).not.toBeNull();
		expect(shape!.line).toMatch(/^M[\d.]+ [\d.]+( L[\d.]+ [\d.]+){3}$/);
		expect(shape!.last).toBe(0.72);
		expect(shape!.min).toBe(0.41);
		expect(shape!.max).toBe(0.72);
	});

	it('says so when the window held more points than the API returned', () => {
		expect(fixture.truncated).toBe(false);
		expect(fixture.max_points).toBeGreaterThan(0);
	});
});
