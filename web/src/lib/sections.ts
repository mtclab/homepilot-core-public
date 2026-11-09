// The route tabs of the sections that carry views (UI slice 1). Kept as data
// next to Changes' and Records' so "every tab reaches a page" and "the
// current one is marked" are assertable without mounting a layout.

import type { RouteTabDef } from './tabs';

/** Monitoring: alerts first (the order of urgency), then the rules. */
export const MONITORING_TABS: RouteTabDef[] = [
	{ id: 'alerts', label: 'Alerts', href: '/monitoring/alerts' },
	{ id: 'rules', label: 'Rules', href: '/monitoring/rules' },
];

/**
 * Hosts: the fleet list and the agent system's chores (owner decision 2:
 * Agents is a tab under Hosts, not a noun). Both exact: `/hosts/{id}` is a
 * host page, which carries its own tabs and belongs to neither of these.
 */
export const HOSTS_TABS: RouteTabDef[] = [
	{ id: 'fleet', label: 'Fleet', href: '/hosts', exact: true },
	{ id: 'agents', label: 'Agents', href: '/hosts/agents', exact: true },
];

/**
 * Changes: the Inbox first (UI_PLAN 3.6: proposals AND approved changes still
 * to run), then every change, then drift. The layout adds the waiting count to
 * the Inbox label; the command palette offers these same views (3.10.0: the
 * palette kept its own list and still offered the retired review queue).
 */
export const CHANGES_TABS: RouteTabDef[] = [
	{ id: 'inbox', label: 'Inbox', href: '/changes/inbox' },
	{ id: 'artifacts', label: 'All', href: '/changes', exact: true },
	{ id: 'drift', label: 'Drift', href: '/changes/drift' },
];

/** Records: the evidence surfaces - what ran, what was done, what we know. */
export const RECORDS_TABS: RouteTabDef[] = [
	{ id: 'tasks', label: 'Tasks', href: '/records/tasks' },
	{ id: 'journal', label: 'Journal', href: '/records/journal' },
	{ id: 'kb', label: 'Knowledge base', href: '/records/kb' },
];
