// The Host page's sectors (#549 F3, principle 2) and the legacy links into them.
//
// The host page grew five stacked sections (#514 S2/S4) that an operator had to
// scroll past to reach the one they came for. They become `?tab=` tabs on the
// same route - the id is already in the path, so the sector is query state.
//
// The addressing lives here, next to the tab list, because the promise that
// matters is "every link that used to work still lands in the right place" and
// that promise has to be assertable without a DOM.

import type { QueryTabDef } from './tabs';

export const HOST_TABS: QueryTabDef[] = [
	{ id: 'overview', label: 'Overview' },
	{ id: 'metrics', label: 'Metrics' },
	{ id: 'changes', label: 'Changes' },
	{ id: 'activity', label: 'Activity' },
	{ id: 'agent', label: 'Agent' },
];

/**
 * Before the tabs the sections were one long page, so a colleague's link into
 * one is a fragment: `/ui/hosts/{id}#agent`. Those fragments keep working -
 * they resolve to the tab that swallowed the section, and the page rewrites the
 * URL to the `?tab=` form so the next copy-paste carries the canonical link.
 *
 * `facts`/`services`/`notes` were their own headings and all live on Overview
 * now; `#metrics` and friends map to themselves.
 */
const LEGACY_ANCHORS: Record<string, string> = {
	overview: 'overview',
	facts: 'overview',
	identity: 'overview',
	services: 'overview',
	notes: 'overview',
	metrics: 'metrics',
	changes: 'changes',
	artifacts: 'changes',
	activity: 'activity',
	agent: 'agent',
};

/**
 * The tab a legacy fragment belongs to, or `''` when the fragment names nothing
 * this page ever had. An unknown fragment must NOT be turned into a redirect:
 * leaving it alone lets the normal `?tab=` resolution (which falls back to
 * Overview) answer, rather than inventing a sector.
 */
export function hostTabFromHash(hash: string): string {
	const key = hash.replace(/^#/, '').toLowerCase();
	return LEGACY_ANCHORS[key] ?? '';
}
