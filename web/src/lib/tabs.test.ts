import { describe, it, expect } from 'vitest';
import {
	routeTabs,
	activeRouteTab,
	queryTabs,
	activeQueryTab,
	type RouteTabDef,
} from './tabs';

/**
 * Tab addressing (#549 F1).
 *
 * The failure this forbids: a tab that is not a URL. The console's tabs are how
 * an operator sends "look at the drift on web-01" to a colleague, and how a
 * bookmark survives a reorganisation. If the tab bar ever becomes local
 * component state, that link dies silently - every tab still looks fine.
 *
 * The route cases below are the EXACT behaviour the hand-rolled Changes and
 * Records layouts had before the extraction, including the one subtlety worth
 * keeping: an artifact detail page is still "Artifacts".
 */
const BASE = '/ui';

const CHANGES: RouteTabDef[] = [
	{ id: 'artifacts', label: 'Artifacts', href: '/changes', exact: true },
	{ id: 'review', label: 'Review queue', href: '/changes/review' },
	{ id: 'drift', label: 'Drift', href: '/changes/drift' },
];

const RECORDS: RouteTabDef[] = [
	{ id: 'tasks', label: 'Tasks', href: '/records/tasks' },
	{ id: 'journal', label: 'Journal', href: '/records/journal' },
	{ id: 'kb', label: 'Knowledge base', href: '/records/kb' },
];

describe('route tabs', () => {
	it('applies base once, so a link is a real in-app URL', () => {
		expect(routeTabs(CHANGES, BASE).map((t) => t.href)).toEqual([
			'/ui/changes',
			'/ui/changes/review',
			'/ui/changes/drift',
		]);
	});

	const CASES: Array<[string, string]> = [
		['/ui/changes', 'artifacts'],
		['/ui/changes/', 'artifacts'],
		['/ui/changes/review', 'review'],
		['/ui/changes/review/', 'review'],
		['/ui/changes/drift', 'drift'],
		// A detail route under the group belongs to the group's first tab.
		['/ui/changes/abc-123', 'artifacts'],
	];

	for (const [path, expected] of CASES) {
		it(`${path} is the "${expected}" tab`, () => {
			expect(activeRouteTab(CHANGES, path, BASE, BASE + '/changes/')).toBe(expected);
		});
	}

	it('the index tab does not swallow its siblings', () => {
		// The exact flag is the whole reason /changes/drift is not "Artifacts".
		expect(activeRouteTab(CHANGES, '/ui/changes/drift', BASE, BASE + '/changes/')).not.toBe(
			'artifacts',
		);
	});

	it('marks nothing current when the URL is outside the group', () => {
		expect(activeRouteTab(CHANGES, '/ui/hosts', BASE, BASE + '/changes/')).toBe('');
		expect(activeRouteTab(RECORDS, '/ui/settings', BASE)).toBe('');
	});

	it('a records sub-route and its children resolve to their own tab', () => {
		expect(activeRouteTab(RECORDS, '/ui/records/tasks', BASE)).toBe('tasks');
		expect(activeRouteTab(RECORDS, '/ui/records/tasks/17', BASE)).toBe('tasks');
		expect(activeRouteTab(RECORDS, '/ui/records/kb', BASE)).toBe('kb');
	});

	it('a sibling route that merely SHARES a prefix is not the tab', () => {
		// `startsWith` without the boundary would call /records/tasksy "Tasks".
		expect(activeRouteTab(RECORDS, '/ui/records/tasksy', BASE)).toBe('');
	});
});

describe('query tabs', () => {
	const HOST = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'metrics', label: 'Metrics' },
		{ id: 'agent', label: 'Agent' },
	];

	it('every tab is a link on the page own path', () => {
		expect(queryTabs(HOST, '/ui/hosts/9').map((t) => t.href)).toEqual([
			'/ui/hosts/9?tab=overview',
			'/ui/hosts/9?tab=metrics',
			'/ui/hosts/9?tab=agent',
		]);
	});

	it('keeps the rest of the query, so switching tab does not drop a filter', () => {
		const search = new URLSearchParams({ q: 'cpu', tab: 'overview' });
		expect(queryTabs(HOST, '/ui/hosts/9', 'tab', search)[1].href).toBe(
			'/ui/hosts/9?q=cpu&tab=metrics',
		);
	});

	it('reads the current tab out of the URL', () => {
		expect(activeQueryTab(HOST, new URL('http://x/ui/hosts/9?tab=metrics'))).toBe('metrics');
	});

	it('falls back to the first tab for a missing or stale value', () => {
		expect(activeQueryTab(HOST, new URL('http://x/ui/hosts/9'))).toBe('overview');
		expect(activeQueryTab(HOST, new URL('http://x/ui/hosts/9?tab=retired'))).toBe('overview');
	});
});
