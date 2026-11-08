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
