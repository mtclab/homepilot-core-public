// The drift rollup (#549 F4, principle 1: attention before enumeration).
//
// The Drift tab used to print every active artifact in two tables — the
// forty that agree with reality alongside the two that do not — so the page
// that exists to say "these two need you" said it in a wall of green ticks.
//
// This is the pure half of the fix: what gets ENUMERATED (artifacts whose
// reality disagrees, and checks that established nothing) and what collapses
// to ONE line (the in-spec ones, the never-checked ones, the orphans).
//
// The #425 honesty rules are enforced here, not in the markup:
//   * a check that ERRORED is not "in spec" — it is its own, enumerated class;
//   * an artifact that was never checked is neither — it is counted, not green;
//   * with nothing checked at all there is no percentage to quote, so
//     `coveragePct` is null and the page has no number to round up.

import type { Artifact, DriftCheck, Host } from '$lib/api';

export type DriftState = 'drifted' | 'in-spec' | 'unknown' | 'unchecked';

/** Statuses whose reality can disagree with the artifact — the drift universe. */
export const ACTIVE_STATUSES = ['applied', 'approved'];

export interface DriftItem {
	artifact: Artifact;
	/** The artifact's target name, '' when it targets nothing in particular. */
	target: string;
	/** The inventory host the target resolves to, if any. */
	host: Host | null;
	state: DriftState;
	check: DriftCheck | null;
	/** One line: what disagrees, or why nothing could be established. */
	summary: string;
	/** Targets a name that is absent from inventory. */
	orphan: boolean;
}

export interface DriftRollup {
	/** Enumerated: reality disagrees. */
	drifted: DriftItem[];
	/** Enumerated: the check ran and established nothing (#425 `unknown`). */
	unresolved: DriftItem[];
	/** NOT enumerated: one line, with the freshest check behind it. */
	inSpec: { count: number; lastCheckedAt: string | null };
	/** NOT enumerated: one line. */
	uncheckedCount: number;
	checkedCount: number;
	total: number;
	/** How many active artifacts point at a name inventory does not know. */
	orphanCount: number;
	/** null when nothing has been checked — there is no honest percentage then. */
	coveragePct: number | null;
	/** In inventory, targeted by no active artifact. */
	uncoveredHosts: Host[];
}

export function artifactTarget(a: Artifact): string {
	const t = a.target ?? {};
	return t.host ?? t.service ?? t.node ?? '';
}

/** What the stored check actually established. Absent check => never checked. */
export function checkState(dc: DriftCheck | null): DriftState {
	if (!dc) return 'unchecked';
	// `drifted: false` used to cover both "I looked and it matches" and "I could
	// not look" (#425); only `state` separates them.
	if (dc.state === 'drifted') return 'drifted';
	if (dc.state === 'in_spec') return 'in-spec';
	return 'unknown';
}

function parseDetails(dc: DriftCheck | null): Record<string, unknown> {
	if (!dc?.details_json) return {};
	try {
		const parsed: unknown = JSON.parse(dc.details_json);
		return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {};
	} catch {
		return {};
	}
}

function names(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

/**
 * The diff summary an operator reads instead of opening the artifact: WHICH
 * items/steps/sub-artifacts disagree, or why a check established nothing.
 */
export function driftSummary(state: DriftState, dc: DriftCheck | null): string {
	const details = parseDetails(dc);
	if (state === 'unknown') {
		const reason = typeof details.reason === 'string' ? details.reason.replace(/_/g, ' ') : '';
		return reason || 'the check established nothing';
	}
	if (state !== 'drifted') return '';
	const differing = [
		...names(details.drifted_items),
		...names(details.drifted_steps),
		...names(details.drifted_subs),
	];
	if (differing.length === 0) return 'drift detected';
	const shown = differing.slice(0, 3).join(', ');
	const rest = differing.length - 3;
	const noun = differing.length === 1 ? 'difference' : 'differences';
	return `${differing.length} ${noun}: ${shown}${rest > 0 ? ` +${rest} more` : ''}`;
}

/** The freshest `checked_at` of a set of checks, or null. */
function newest(checks: DriftCheck[]): string | null {
	let best: string | null = null;
	for (const c of checks) {
		if (!c.checked_at) continue;
		if (best === null || new Date(c.checked_at) > new Date(best)) best = c.checked_at;
	}
	return best;
}

export function buildDriftRollup(
	artifacts: Artifact[],
	hosts: Host[],
	checks: DriftCheck[],
): DriftRollup {
	const active = artifacts.filter((a) => ACTIVE_STATUSES.includes(a.status));
	const hostByName = new Map<string, Host>();
	for (const h of hosts) {
		hostByName.set(h.hostname, h);
		if (h.node) hostByName.set(h.node, h);
	}
	const checkById = new Map<string, DriftCheck>();
	for (const c of checks) checkById.set(c.artifact_id, c);

	const items: DriftItem[] = active.map((a) => {
		const target = artifactTarget(a);
		const host = target ? (hostByName.get(target) ?? null) : null;
		const check = checkById.get(a.id) ?? null;
		const state = checkState(check);
		return {
			artifact: a,
			target,
			host,
			state,
			check,
			summary: driftSummary(state, check),
			// A GLOBAL artifact targets nothing, so it cannot be orphaned; only a
			// named target inventory does not know is.
			orphan: target !== '' && host === null,
		};
	});

	const drifted = items.filter((i) => i.state === 'drifted');
	const unresolved = items.filter((i) => i.state === 'unknown');
	const inSpecItems = items.filter((i) => i.state === 'in-spec');
	const uncheckedCount = items.filter((i) => i.state === 'unchecked').length;
	const checkedCount = drifted.length + unresolved.length + inSpecItems.length;

	const covered = new Set(items.map((i) => i.host?.id).filter(Boolean));

	return {
		drifted,
		unresolved,
		inSpec: {
			count: inSpecItems.length,
			lastCheckedAt: newest(inSpecItems.map((i) => i.check).filter((c): c is DriftCheck => !!c)),
		},
		uncheckedCount,
		checkedCount,
		total: items.length,
		orphanCount: items.filter((i) => i.orphan).length,
		// Percentage of the fleet's artifacts that were actually checked. Quoting
		// one when nothing was checked is the "100% healthy" lie #425 is about.
		coveragePct: checkedCount === 0 ? null : Math.round((checkedCount / items.length) * 100),
		uncoveredHosts: hosts.filter((h) => !covered.has(h.id)),
	};
}
