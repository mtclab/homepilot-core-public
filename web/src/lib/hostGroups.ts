// Grouping the fleet (#549 F3, principle 1: attention before enumeration).
//
// A flat list of every machine in the estate, sorted by nothing an operator
// cares about, is the "long lists of things" the owner complained about. The
// fleet has three populations and they want different things from the reader:
//
//   1. NEEDS ATTENTION - a managed machine that is offline, whose agent stopped
//      connecting, or that the hypervisor no longer reports at all. These are
//      the rows an operator opened the page for.
//   2. MANAGED - adopted and behaving. Rolled up, collapsible, still there.
//   3. DISCOVERED - seen but not adopted (pending or deliberately ignored).
//      A decision queue, not a fault.
//
// Kept as pure functions rather than inline `{#if}` chains on the page so the
// ORDER and the membership rules can be asserted without mounting a table.

import type { Host } from './api';

export type HostGroupId = 'attention' | 'managed' | 'discovered';

export interface HostGroup {
	id: HostGroupId;
	label: string;
	hosts: Host[];
}

/**
 * A host HomePilot has not been told to manage: discovered and still pending, or
 * explicitly ignored. Adoption - not the source - is the question, because a
 * hand-added host is adopted the moment it is created and a Proxmox-discovered
 * one may never be.
 */
export function isUnadopted(h: Host): boolean {
	return h.import_state === 'pending' || h.import_state === 'ignored';
}

/**
 * Why this host needs an operator's eyes NOW, as the chip text; `''` when it
 * does not.
 *
 * Only MANAGED machines can raise attention. An un-adopted discovered guest
 * that happens to be powered off is not a fault - it is a machine nobody asked
 * HomePilot to care about, and letting it shout is how an attention zone
 * becomes wallpaper.
 *
 * NOTE: drift is deliberately absent. The fleet list payload carries no drift
 * state (`DriftCheck` keys on artifact_id, not on a host), so a drift chip here
 * could only be guessed. The HOST page, which does know this machine's
 * artifacts, raises it there instead.
 */
export function attentionReason(h: Host): string {
	if (isUnadopted(h)) return '';
	// Ordered by how final the news is: gone outranks offline outranks a dead
	// agent channel, and a row shows exactly one reason.
	if (h.absent_since) return 'gone';
	if (h.status === 'offline') return 'offline';
	if (h.agent_id && h.agent_connected === false) return 'agent offline';
	return '';
}

export function groupOf(h: Host): HostGroupId {
	if (isUnadopted(h)) return 'discovered';
	return attentionReason(h) ? 'attention' : 'managed';
}

const LABELS: Record<HostGroupId, string> = {
	attention: 'Needs attention',
	managed: 'Managed',
	discovered: 'Discovered',
};

/**
 * The groups, in priority order, with EMPTY ONES DROPPED - an empty "Needs
 * attention" heading is a false alarm rendered every single page load.
 * Membership order inside a group is the server's order, untouched.
 */
export function groupHosts(hosts: Host[]): HostGroup[] {
	const order: HostGroupId[] = ['attention', 'managed', 'discovered'];
	const buckets: Record<HostGroupId, Host[]> = { attention: [], managed: [], discovered: [] };
	for (const h of hosts) buckets[groupOf(h)].push(h);
	return order
		.filter((id) => buckets[id].length > 0)
		.map((id) => ({ id, label: LABELS[id], hosts: buckets[id] }));
}

/**
 * Above this many hosts on the page, the healthy group starts collapsed: past
 * roughly a screenful the managed rows are enumeration, and the two groups that
 * want a decision get pushed off the fold by machines that are fine.
 */
export const COLLAPSE_ABOVE = 10;

/** Which groups collapse at all. Attention never does; nothing may hide a fault. */
export function isCollapsible(id: HostGroupId): boolean {
	return id === 'managed';
}

const STORE_KEY = 'hp.hosts.collapsed';

/**
 * The operator's own choice, remembered per browser. Absent means "no choice
 * yet", which is what lets the fleet size decide the default - a stored `false`
 * (they opened it) has to survive the fleet growing past the threshold.
 *
 * Every access is guarded: private mode, a blocked-storage policy and a
 * server-side render all throw or have no `localStorage` at all, and a fleet
 * page that cannot render because of a UI preference is a worse bug than the
 * preference being forgotten.
 */
export function readCollapsePreference(id: HostGroupId): boolean | null {
	try {
		const raw = globalThis.localStorage?.getItem(`${STORE_KEY}.${id}`);
		if (raw === 'true') return true;
		if (raw === 'false') return false;
		return null;
	} catch {
		return null;
	}
}

export function writeCollapsePreference(id: HostGroupId, collapsed: boolean): void {
	try {
		globalThis.localStorage?.setItem(`${STORE_KEY}.${id}`, collapsed ? 'true' : 'false');
	} catch {
		// A remembered preference is a nicety; losing it must never break the page.
	}
}

/** Collapsed by default? The stored choice wins; otherwise fleet size decides. */
export function initialCollapsed(id: HostGroupId, fleetSize: number): boolean {
	if (!isCollapsible(id)) return false;
	const stored = readCollapsePreference(id);
	if (stored !== null) return stored;
	return fleetSize > COLLAPSE_ABOVE;
}
