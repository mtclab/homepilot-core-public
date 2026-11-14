// The quiet Overview's rules that live in the browser (3.12).
//
// The rules that need the whole task table, the hub registry or the snooze
// table are the server's (src/homepilot/dashboard/attention.py) and arrive in
// the summary's `attention`. What is here is presentation plus the two rules
// the browser must apply to the lines it still builds from its other reads
// (alerts, drift, enrolment requests): whether a snooze holds, and which
// machines count as running. Each MUST equal its server twin, named below.

import type { AgentUpgrade, AttentionSeverity, AttentionSnooze, Host, Task } from './api';

export const SEVERITY_RANK: Record<AttentionSeverity, number> = { notice: 0, warning: 1, critical: 2 };

/**
 * Does this snooze still hide a line of this severity? Until its `until`, and
 * only while the line is no MORE serious than when it was put off.
 * MUST equal `snooze_holds` in dashboard/attention.py.
 */
export function snoozeHolds(
	snooze: AttentionSnooze | undefined,
	severity: AttentionSeverity,
	now: number = Date.now(),
): boolean {
	if (!snooze) return false;
	const until = Date.parse(snooze.until);
	if (Number.isNaN(until) || now >= until) return false;
	return (SEVERITY_RANK[severity] ?? 3) <= (SEVERITY_RANK[snooze.severity] ?? -1);
}

/**
 * A machine that is up: Proxmox says running, or - for one Proxmox does not
 * describe (a hand-added host, a node) - its own status says so. Never a gone
 * one. MUST equal `is_running` in dashboard/service.py.
 */
export function isRunning(h: Host): boolean {
	if (h.absent_since || h.is_template) return false;
	const pve = h.pve_status ?? '';
	if (pve === 'running') return true;
	return (pve === '' || pve === 'unknown') && (h.status === 'online' || h.status === 'running');
}

export function plural(n: number, one: string, many: string): string {
	return `${n} ${n === 1 ? one : many}`;
}

/** "20 Nov": a date in words, no year (a snooze is at most a week out). */
export function dayMonth(iso: string): string {
	const t = Date.parse(iso);
	if (Number.isNaN(t)) return iso;
	return new Date(t).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
}

/** The footer's account of the failed runs it did not ask about; '' when none. */
export function leftOutSentence(left: { superseded: number; older: number } | undefined): string {
	const sup = left?.superseded ?? 0;
	const old = left?.older ?? 0;
	if (sup && old)
		return `${plural(sup, 'failed run was', 'failed runs were')} superseded by later successes and ${old} ${old === 1 ? 'is' : 'are'} more than a day old; all are in Records.`;
	if (sup) return `${plural(sup, 'failed run was', 'failed runs were')} superseded by later successes and ${sup === 1 ? 'is' : 'are'} in Records.`;
	if (old) return `${plural(old, 'failed run', 'failed runs')} more than a day old ${old === 1 ? 'is' : 'are'} in Records.`;
	return '';
}

/** "2 put off until 20 Nov" for the lines a snooze is holding back now. */
export function putOffSentence(held: AttentionSnooze[]): string {
	if (!held.length) return '';
	const days = [...new Set(held.map((s) => dayMonth(s.until)))];
	const until = days.length === 1 ? `until ${days[0]}` : `until ${days.join(', ')}`;
	return `${held.length} put off ${until}`;
}

export interface ForgetOutcome {
	forgotten: string[];
	failed: { hostname: string; error: string }[];
}

/**
 * "Forget all N": the Hosts page's forget, once per machine the line names,
 * in turn. All of them are tried; what could not be forgotten is reported by
 * name and reason, never dropped.
 */
export async function forgetAll(
	hosts: { id: string; hostname: string }[],
	forget: (id: string) => Promise<unknown>,
): Promise<ForgetOutcome> {
	const out: ForgetOutcome = { forgotten: [], failed: [] };
	for (const h of hosts) {
		try {
			await forget(h.id);
			out.forgotten.push(h.hostname);
		} catch (e) {
			out.failed.push({ hostname: h.hostname, error: e instanceof Error ? e.message : String(e) });
		}
	}
	return out;
}

export type UpgradeState = 'waiting' | 'upgrading' | 'done' | 'failed' | 'skipped';

export interface UpgradeOutcome {
	done: string[];
	/** The host it stopped at, and why. Absent when every one went through. */
	stopped?: { hostname: string; error: string };
}

/**
 * "Upgrade N agents": the per-agent upgrade (POST /agents/install with
 * upgrade), ONE AT A TIME, each followed to its end before the next starts.
 * Stops at the first failure and says which host and why - an old agent's
 * first upgrade can fail (#823), and the next host must not be tried as if it
 * had not.
 */
export async function upgradeInTurn(
	rows: AgentUpgrade[],
	deps: {
		install: (hostId: string) => Promise<{ task_id: string }>;
		getTask: (taskId: string) => Promise<Task>;
		wait?: (ms: number) => Promise<void>;
	},
	onState: (agentId: string, state: UpgradeState, note?: string) => void,
): Promise<UpgradeOutcome> {
	const wait = deps.wait ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
	const done: string[] = [];
	for (const row of rows) {
		if (!row.host_id) {
			onState(row.agent_id, 'skipped', 'not a QEMU guest HomePilot can reach');
			continue;
		}
		onState(row.agent_id, 'upgrading');
		let error = '';
		try {
			const { task_id } = await deps.install(row.host_id);
			for (;;) {
				const task = await deps.getTask(task_id);
				if (task.status === 'succeeded') break;
				if (task.status !== 'pending' && task.status !== 'running') {
					error = task.error || `the upgrade ended ${task.status}`;
					break;
				}
				await wait(3000);
			}
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
		if (error) {
			onState(row.agent_id, 'failed', error);
			return { done, stopped: { hostname: row.hostname, error } };
		}
		onState(row.agent_id, 'done');
		done.push(row.hostname);
	}
	return { done };
}
