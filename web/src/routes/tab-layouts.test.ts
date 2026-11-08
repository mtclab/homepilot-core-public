import { render, screen, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, afterEach } from 'vitest';
import type { Writable } from 'svelte/store';

/**
 * Changes and Records after the TabBar extraction (#549 F1).
 *
 * This is the anti-regression half of the slice. F1 replaced two hand-rolled
 * tab rows with one component; the rows had NO tests, so "the extraction did
 * not change behaviour" was an unbacked claim. These assert the behaviour that
 * matters and that the hand-rolled versions had:
 *
 *   - the same three views, in the same order, pointing at the same URLs;
 *   - the right one marked current for every route in the group, including the
 *     artifact DETAIL route (which belongs to Artifacts and to no other tab);
 *   - and now, additionally, the roles that make the row a tab bar.
 *
 * `$app/stores` is mocked locally rather than through `$lib/test-mocks`: these
 * layouts are ABOUT the URL, so the URL has to vary per case.
 */
// The mock factory runs while the layouts are imported, which is BEFORE any
// top-level const in this file is initialised - hence the hoisted holder.
const held = vi.hoisted(() => ({ page: null as Writable<{ url: URL }> | null }));

vi.mock('$app/paths', () => ({ base: '/ui' }));
vi.mock('$app/stores', async () => {
	const { writable } = await import('svelte/store');
	held.page = writable({ url: new URL('http://localhost/ui/changes') });
	return { page: held.page };
});

import ChangesLayout from './changes/+layout.svelte';
import RecordsLayout from './records/+layout.svelte';
import HostsLayout from './hosts/+layout.svelte';
import MonitoringLayout from './monitoring/+layout.svelte';

function at(path: string): void {
	held.page!.set({ url: new URL('http://localhost' + path) });
}

afterEach(() => cleanup());

describe('Changes tab bar', () => {
	it('keeps its three views, in order, at their old URLs', () => {
		at('/ui/changes');
		render(ChangesLayout);
		expect(screen.getAllByRole('tab').map((t) => [t.textContent, t.getAttribute('href')])).toEqual(
			[
				['Artifacts', '/ui/changes'],
				['Review queue', '/ui/changes/review'],
				['Drift', '/ui/changes/drift'],
			],
		);
	});

	const CURRENT: Array<[string, string]> = [
		['/ui/changes', 'Artifacts'],
		['/ui/changes/review', 'Review queue'],
		['/ui/changes/drift', 'Drift'],
		// The artifact detail page is still the Artifacts view.
		['/ui/changes/a1b2c3', 'Artifacts'],
	];

	for (const [path, label] of CURRENT) {
		it(`${path} marks "${label}" current`, () => {
			at(path);
			render(ChangesLayout);
			const selected = screen
				.getAllByRole('tab')
				.filter((t) => t.getAttribute('aria-selected') === 'true');
			expect(selected.map((t) => t.textContent)).toEqual([label]);
		});
	}

	it('is a real tab bar with a labelled panel', () => {
		at('/ui/changes/drift');
		render(ChangesLayout);
		expect(screen.getByRole('tablist', { name: 'Changes views' })).toBeInTheDocument();
		const panel = screen.getByRole('tabpanel');
		expect(panel).toHaveAttribute('id', 'changes-panel');
		expect(panel).toHaveAttribute('aria-labelledby', 'tab-drift');
		expect(screen.getByRole('tab', { name: 'Drift' })).toHaveAttribute(
			'aria-controls',
			'changes-panel',
		);
	});
});

describe('Records tab bar', () => {
	it('keeps its three views, in order, at their old URLs', () => {
		at('/ui/records/tasks');
		render(RecordsLayout);
		expect(screen.getAllByRole('tab').map((t) => [t.textContent, t.getAttribute('href')])).toEqual(
			[
				['Tasks', '/ui/records/tasks'],
				['Journal', '/ui/records/journal'],
				['Knowledge base', '/ui/records/kb'],
			],
		);
	});

	const CURRENT: Array<[string, string]> = [
		['/ui/records/tasks', 'Tasks'],
		['/ui/records/journal', 'Journal'],
		['/ui/records/kb', 'Knowledge base'],
	];

	for (const [path, label] of CURRENT) {
		it(`${path} marks "${label}" current`, () => {
			at(path);
			render(RecordsLayout);
			const selected = screen
				.getAllByRole('tab')
				.filter((t) => t.getAttribute('aria-selected') === 'true');
			expect(selected.map((t) => t.textContent)).toEqual([label]);
		});
	}

	it('is a real tab bar with a labelled panel', () => {
		at('/ui/records/kb');
		render(RecordsLayout);
		expect(screen.getByRole('tablist', { name: 'Records views' })).toBeInTheDocument();
		expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'tab-kb');
	});
});

// UI slice 1: two more sections carry views. Teeth: make a Hosts tab
// non-exact and the host page grows a tab bar it does not own; reorder or drop
// a view and the order cases fail.

describe('Hosts tab bar', () => {
	it('has Fleet and Agents, at their URLs', () => {
		at('/ui/hosts');
		render(HostsLayout);
		expect(screen.getAllByRole('tab').map((t) => [t.textContent, t.getAttribute('href')])).toEqual(
			[
				['Fleet', '/ui/hosts'],
				['Agents', '/ui/hosts/agents'],
			],
		);
	});

	for (const [path, label] of [
		['/ui/hosts', 'Fleet'],
		['/ui/hosts/agents', 'Agents'],
	] as Array<[string, string]>) {
		it(`${path} marks "${label}" current`, () => {
			at(path);
			render(HostsLayout);
			const selected = screen
				.getAllByRole('tab')
				.filter((t) => t.getAttribute('aria-selected') === 'true');
			expect(selected.map((t) => t.textContent)).toEqual([label]);
		});
	}

	it('a host page is neither view and carries no Hosts tab bar', () => {
		at('/ui/hosts/a1b2c3');
		render(HostsLayout);
		expect(screen.queryByRole('tablist', { name: 'Hosts views' })).toBeNull();
	});
});

describe('Monitoring tab bar', () => {
	it('has Alerts then Rules, at their URLs, under one page title', () => {
		at('/ui/monitoring/alerts');
		render(MonitoringLayout);
		expect(screen.getByRole('heading', { level: 1, name: 'Monitoring' })).toBeInTheDocument();
		expect(screen.getAllByRole('tab').map((t) => [t.textContent, t.getAttribute('href')])).toEqual(
			[
				['Alerts', '/ui/monitoring/alerts'],
				['Rules', '/ui/monitoring/rules'],
			],
		);
	});

	for (const [path, label] of [
		['/ui/monitoring/alerts', 'Alerts'],
		['/ui/monitoring/rules', 'Rules'],
	] as Array<[string, string]>) {
		it(`${path} marks "${label}" current`, () => {
			at(path);
			render(MonitoringLayout);
			const selected = screen
				.getAllByRole('tab')
				.filter((t) => t.getAttribute('aria-selected') === 'true');
			expect(selected.map((t) => t.textContent)).toEqual([label]);
		});
	}
});
