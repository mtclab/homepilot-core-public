import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import TabBar from './TabBar.svelte';
import { routeTabs, queryTabs } from '$lib/tabs';

/**
 * The shared tab bar (#549 F1).
 *
 * Two failures this forbids:
 *
 *  1. A tab that is not addressable. Both modes - sub-route tabs (Changes,
 *     Records) and `?tab=` tabs (the Host page) - must render REAL links, so a
 *     tab can be bookmarked, shared and reached by Back. Asserting the href is
 *     asserting the operator can actually hand the view to someone else.
 *  2. A tab bar that only works with a mouse. The old hand-rolled rows were a
 *     flex of anchors with no roles and no arrow-key walk: a screen reader
 *     announced three unrelated links and never said which one was current.
 */
const ROUTE_TABS = routeTabs(
	[
		{ id: 'artifacts', label: 'Artifacts', href: '/changes', exact: true },
		{ id: 'review', label: 'Review queue', href: '/changes/review' },
		{ id: 'drift', label: 'Drift', href: '/changes/drift' },
	],
	'/ui',
);

const QUERY_TABS = queryTabs(
	[
		{ id: 'overview', label: 'Overview' },
		{ id: 'metrics', label: 'Metrics' },
		{ id: 'agent', label: 'Agent' },
	],
	'/ui/hosts/9',
);

describe('TabBar: both modes are URL-addressable', () => {
	it('sub-route mode renders one link per route', () => {
		render(TabBar, { tabs: ROUTE_TABS, activeId: 'review', label: 'Changes views' });
		expect(screen.getByRole('tab', { name: 'Artifacts' })).toHaveAttribute(
			'href',
			'/ui/changes',
		);
		expect(screen.getByRole('tab', { name: 'Drift' })).toHaveAttribute(
			'href',
			'/ui/changes/drift',
		);
	});

	it('?tab= mode renders one link per sector on the same route', () => {
		render(TabBar, { tabs: QUERY_TABS, activeId: 'overview', label: 'Host views' });
		expect(screen.getByRole('tab', { name: 'Metrics' })).toHaveAttribute(
			'href',
			'/ui/hosts/9?tab=metrics',
		);
	});
});

describe('TabBar: the current tab is announced, not just coloured', () => {
	it('marks exactly the active tab selected', () => {
		render(TabBar, { tabs: ROUTE_TABS, activeId: 'drift', label: 'Changes views' });
		const tabs = screen.getAllByRole('tab');
		expect(tabs.map((t) => t.getAttribute('aria-selected'))).toEqual(['false', 'false', 'true']);
	});

	it('the active tab also carries the accent idiom, not only the ARIA state', () => {
		render(TabBar, { tabs: ROUTE_TABS, activeId: 'drift', label: 'Changes views' });
		expect(screen.getByRole('tab', { name: 'Drift' }).className).toContain('text-accent');
		expect(screen.getByRole('tab', { name: 'Artifacts' }).className).toContain('text-muted');
	});

	it('names the row and points every tab at its panel', () => {
		render(TabBar, {
			tabs: ROUTE_TABS,
			activeId: 'review',
			label: 'Changes views',
			panelId: 'changes-panel',
		});
		expect(screen.getByRole('tablist', { name: 'Changes views' })).toBeInTheDocument();
		for (const tab of screen.getAllByRole('tab')) {
			expect(tab).toHaveAttribute('aria-controls', 'changes-panel');
		}
	});

	it('keeps exactly one tab in the tab order (roving tabindex)', () => {
		render(TabBar, { tabs: ROUTE_TABS, activeId: 'review', label: 'Changes views' });
		expect(screen.getAllByRole('tab').map((t) => t.getAttribute('tabindex'))).toEqual([
			'-1',
			'0',
			'-1',
		]);
	});

	it('stays keyboard-reachable when the URL matches no tab', () => {
		// A detail route with no tab of its own must not orphan the whole row.
		render(TabBar, { tabs: ROUTE_TABS, activeId: '', label: 'Changes views' });
		const order = screen.getAllByRole('tab').map((t) => t.getAttribute('tabindex'));
		expect(order.filter((t) => t === '0')).toHaveLength(1);
	});
});

describe('TabBar: keyboard walk (WAI-ARIA tabs pattern)', () => {
	async function focusFirst() {
		render(TabBar, { tabs: ROUTE_TABS, activeId: 'artifacts', label: 'Changes views' });
		const tabs = screen.getAllByRole('tab');
		tabs[0].focus();
		return tabs;
	}

	it('arrow right and left move along the row', async () => {
		const user = userEvent.setup();
		const tabs = await focusFirst();

		await user.keyboard('{ArrowRight}');
		expect(document.activeElement).toBe(tabs[1]);

		await user.keyboard('{ArrowRight}');
		expect(document.activeElement).toBe(tabs[2]);

		await user.keyboard('{ArrowLeft}');
		expect(document.activeElement).toBe(tabs[1]);
	});

	it('wraps at both ends', async () => {
		const user = userEvent.setup();
		const tabs = await focusFirst();

		await user.keyboard('{ArrowLeft}');
		expect(document.activeElement).toBe(tabs[2]);

		await user.keyboard('{ArrowRight}');
		expect(document.activeElement).toBe(tabs[0]);
	});

	it('Home and End jump to the ends', async () => {
		const user = userEvent.setup();
		const tabs = await focusFirst();

		await user.keyboard('{End}');
		expect(document.activeElement).toBe(tabs[2]);

		await user.keyboard('{Home}');
		expect(document.activeElement).toBe(tabs[0]);
	});

	it('leaves other keys alone, so Tab still leaves the row', async () => {
		const user = userEvent.setup();
		const tabs = await focusFirst();
		await user.keyboard('{Tab}');
		expect(document.activeElement).not.toBe(tabs[1]);
	});
});
