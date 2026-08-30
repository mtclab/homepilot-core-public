// How the self-check report is SAID in the UI (#549 F6).
//
// The report itself is written to be truthful per state (src/homepilot/
// selfcheck.py: every entry carries the consequence in plain words), so the web
// side must not paraphrase it - it decides only the name, the chip wording and
// the chip's colour, and renders `consequence` verbatim.
//
// Pure on purpose: "an unreachable subsystem is not a grey mystery chip" is a
// claim about a mapping, and a mapping can be asserted without a DOM.

import type { SelfcheckState, SelfcheckSubsystem } from './api';

/**
 * Human names for the subsystems this instance reports on. The API does not
 * send a label today; `label` is honoured first so a server that starts sending
 * one wins over this list rather than being ignored.
 */
const SUBSYSTEM_LABELS: Record<string, string> = {
	proxmox: 'Proxmox',
	agent_hub: 'Agent hub',
	vault: 'Vault',
	embeddings: 'KB embeddings',
	events_webhook: 'Events webhook',
	mcp: 'MCP',
	artifacts_remote: 'Artifact backup',
	reconcilers: 'Scheduled reconcilers',
	agent_versions: 'Agent versions',
};

export function subsystemLabel(sub: Pick<SelfcheckSubsystem, 'name' | 'label'>): string {
	return sub.label || SUBSYSTEM_LABELS[sub.name] || sub.name.replace(/_/g, ' ');
}

/**
 * What the chip SAYS. `unreachable` spells out that the subsystem is configured
 * - the fault is the reaching, not the absence - and `unknown` says the check
 * did not finish rather than implying the subsystem is broken.
 */
const STATE_TEXT: Record<SelfcheckState, string> = {
	ok: 'ok',
	off: 'off',
	unreachable: 'configured, unreachable',
	unknown: 'unverified',
};

/**
 * The chip's colour, from the existing badge families - no new palette.
 * `unreachable`/`unknown` borrow the SEVERITY chips (#549 F1) because a failing
 * subsystem is an urgency, not a lifecycle state; `ok` and `off` borrow the
 * lifecycle families for their green and their neutral. `off` is deliberately
 * the calm neutral: it is a choice, not a fault.
 */
const STATE_CLASSES: Record<SelfcheckState, string> = {
	ok: 'badge-applied',
	off: 'badge-revoked',
	unreachable: 'badge-critical',
	unknown: 'badge-warning',
};

export function subsystemStateText(state: string): string {
	return STATE_TEXT[state as SelfcheckState] ?? state;
}

export function subsystemStateClass(state: string): string {
	// Never fall through to an unstyled chip: an unrecognised state is unproven,
	// which is exactly what `unknown` means.
	return STATE_CLASSES[state as SelfcheckState] ?? STATE_CLASSES.unknown;
}

/**
 * The address a subsystem is wired to, when naming it helps. `off` has no
 * target worth showing (nothing is configured), and for every other state the
 * operator's next move starts at that address.
 */
export function subsystemTarget(sub: Pick<SelfcheckSubsystem, 'state' | 'target'>): string {
	return sub.state === 'off' ? '' : sub.target;
}
