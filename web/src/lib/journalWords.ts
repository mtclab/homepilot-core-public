// "What happened", in words (3.12, quiet Overview).
//
// The journal row says `withdraw` / `claude (hp_1a2b...)` / an artifact id;
// the operator reads "claude withdrew 4 approved changes". This is the ONE
// place that sentence is built: a table of the actions it knows, and for any
// other action the raw action name as it was recorded - never a guess at what
// it meant. Rows by the same actor doing the same thing one after another are
// one sentence with a count.

import type { AuditEntry } from './api';

/** A piece of a sentence; `strong` is the thing acted on (a host, a change). */
export interface Part {
	text: string;
	strong?: boolean;
}

export interface Happening {
	key: string;
	/** Newest row of the group, ISO. */
	at: string;
	parts: Part[];
	href: string;
	count: number;
}

interface Phrase {
	/** One row: the verb and what it acted on. */
	one: (e: AuditEntry, d: Record<string, unknown>) => Part[];
	/** Several in a row by the same actor: "withdrew 4 approved changes". */
	many?: (n: number) => string;
}

function details(e: AuditEntry): Record<string, unknown> {
	try {
		const parsed: unknown = JSON.parse(e.details_json ?? 'null');
		return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {};
	} catch {
		return {};
	}
}

const w = (text: string): Part => ({ text });
const b = (text: string | null | undefined): Part[] => (text ? [{ text, strong: true }] : []);
const artifact = (verb: string, many?: string): Phrase => ({
	one: (e) => [w(`${verb} `), ...b(e.artifact_id ?? 'a change')],
	many: many ? (n) => `${verb} ${n} ${many}` : undefined,
});
const host = (verb: string, tail = '', many?: string): Phrase => ({
	one: (e) => [w(`${verb} `), ...b(e.target_host ?? 'a host'), ...(tail ? [w(tail)] : [])],
	many: many ? (n) => `${verb} ${n} ${many}` : undefined,
});

/** Every action this knows how to say. Anything else is said as recorded. */
export const PHRASES: Record<string, Phrase> = {
	propose: artifact('proposed', 'changes'),
	approve: artifact('approved', 'changes'),
	reject: artifact('rejected', 'changes'),
	apply: artifact('applied', 'changes'),
	replay: artifact('ran again', 'changes'),
	revoke: artifact('revoked', 'changes'),
	withdraw: artifact('withdrew', 'approved changes'),
	returned_for_review: artifact('sent back to review', 'changes'),
	supersede: artifact('replaced', 'changes'),
	host_added: host('added the host', '', 'hosts'),
	host_forgotten: {
		one: (e, d) => [
			w('forgot '),
			...b(e.target_host ?? 'a host'),
			...(d.reason === 'destroyed by an applied change' ? [w(', which an applied change destroyed')] : []),
		],
		many: (n) => `forgot ${n} machines`,
	},
	install_agent: {
		one: (e, d) =>
			d.upgrade
				? [
						w('upgraded the agent on '),
						...b(e.target_host),
						...(d.agent_version ? [w(` to ${String(d.agent_version)}`)] : []),
					]
				: [w('installed the agent on '), ...b(e.target_host)],
	},
	install_agent_failed: host('could not install the agent on'),
	guest_start: host('started'),
	guest_shutdown: host('shut down'),
	guest_reboot: host('rebooted'),
	guest_stop: host('stopped', ' (forced)'),
	guest_suspend: host('suspended'),
	guest_resume: host('resumed'),
	guest_power_refused: host('was refused a power action on'),
	guest_quota_set: host('set the budget for'),
	guest_quota_removed: host('removed the budget for'),
	guest_invite_minted: host('made an invite for'),
	guest_invite_revoked: host('revoked an invite for'),
	self_guest_confirmed: host('marked', " as HomePilot's own host"),
	self_guest_cleared: { one: () => [w("cleared HomePilot's own host")] },
	webhook_created: { one: () => [w('added a webhook')] },
	webhook_deleted: { one: () => [w('removed a webhook')] },
	webhook_tested: { one: () => [w('tested a webhook')] },
	notification_channel_tested: { one: () => [w('tested a notification channel')] },
	token_minted: { one: () => [w('made a token')], many: (n) => `made ${n} tokens` },
	token_revoked: { one: () => [w('revoked a token')], many: (n) => `revoked ${n} tokens` },
	login: { one: () => [w('signed in')] },
	attention_snoozed: { one: () => [w('put off a line on the Overview')], many: (n) => `put off ${n} lines on the Overview` },
	attention_unsnoozed: { one: () => [w('brought back a line on the Overview')] },
	// The system's own work, behind the toggle.
	check_drift: artifact('checked for drift:', 'changes for drift'),
	check_cycle: { one: () => [w('checked every applied change for drift')], many: (n) => `checked for drift ${n} times` },
	refresh: { one: () => [w('read the inventory from Proxmox')], many: (n) => `read the inventory from Proxmox ${n} times` },
	refresh_incomplete: { one: () => [w('could not finish reading the inventory')] },
	address_conflict: { one: () => [w('found two machines on one address')] },
	address_conflict_resolved: { one: () => [w('saw an address clash clear')] },
	apply_failed: artifact('could not apply', 'changes'),
};

/** The name a journal actor is said by: "You", or its label without the token prefix. */
export function actorName(userId: string, me: { token_label?: string; prefix?: string } | null): string {
	const id = userId || 'system';
	if (me?.token_label) {
		const full = me.prefix ? `${me.token_label} (${me.prefix})` : me.token_label;
		if (id === full || id === me.token_label) return 'You';
	}
	if (id === 'system') return 'HomePilot';
	return id.replace(/\s*\(hp_[A-Za-z0-9]+\)\s*$/, '') || id;
}

function hrefOf(e: AuditEntry, base: string): string {
	return e.artifact_id ? `${base}/changes/${e.artifact_id}` : `${base}/records/journal`;
}

/**
 * Journal rows (newest first) as sentences, newest first. Consecutive rows by
 * the same actor with the same known action become one sentence with a count.
 */
export function describeJournal(
	entries: AuditEntry[],
	me: { token_label?: string; prefix?: string } | null,
	base = '',
): Happening[] {
	const out: Happening[] = [];
	let i = 0;
	while (i < entries.length) {
		const e = entries[i];
		const phrase = PHRASES[e.action];
		let j = i + 1;
		if (phrase?.many) {
			while (j < entries.length && entries[j].action === e.action && entries[j].user_id === e.user_id) j++;
		}
		const n = j - i;
		const who = actorName(e.user_id, me);
		let parts: Part[];
		if (!phrase) {
			// Unknown: the action as recorded, and what it named. Never a paraphrase.
			const on = [e.target_host, e.artifact_id].filter(Boolean).join(', ');
			parts = [w(`${who}: `), { text: e.action, strong: true }, ...(on ? [w(` (${on})`)] : [])];
		} else if (n > 1 && phrase.many) {
			parts = [w(`${who} ${phrase.many(n)}`)];
		} else {
			parts = [w(`${who} `), ...phrase.one(e, details(e))];
		}
		out.push({ key: `audit:${e.id}`, at: e.timestamp, parts, href: hrefOf(e, base), count: n });
		i = j;
	}
	return out;
}

/** The sentence as plain text (for titles, tests and screen readers). */
export function sentence(h: Happening): string {
	return h.parts.map((p) => p.text).join('');
}
