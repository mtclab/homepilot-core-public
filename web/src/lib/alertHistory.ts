// The Alerts list (#660): what is firing, what fired and resolved recently, and
// where alerts are sent. Pure, so the sentences an operator reads can be
// asserted without a DOM or a clock.

import type { AlertEvent, ChannelSummary, FiringAlert } from './api';

export function humanDuration(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '';
	const s = Math.max(0, Math.round(seconds));
	if (s < 90) return `${s} s`;
	if (s < 5400) return `${Math.floor(s / 60)} min`;
	if (s < 172800) return `${Math.floor(s / 3600)} h`;
	return `${Math.floor(s / 86400)} d`;
}

/** What a firing alert is, in words: "went dark" or "load.1m 9 (above 4)". */
export function firingText(a: FiringAlert): string {
	if (a.kind === 'nodata') return `went dark - ${a.name}`;
	const words: Record<string, string> = { gt: 'above', gte: 'at or above', lt: 'below', lte: 'at or below' };
	return `${a.name}: ${a.metric} ${formatValue(a.last_value)} (${words[a.comparison] ?? a.comparison} ${a.threshold})`;
}

function formatValue(v: number | null | undefined): string {
	if (v === null || v === undefined) return '?';
	return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

/** One resolved episode in words: "went dark for 7 min" / "Load too high for 12 min". */
export function resolvedText(e: AlertEvent): string {
	const lasted = humanDuration(e.duration_seconds);
	const forPart = lasted ? ` for ${lasted}` : '';
	if (e.rule_kind === 'nodata') {
		return e.reason ? `counted dark${forPart}, then ${e.reason}` : `went dark${forPart}`;
	}
	return `${e.rule_name}${forPart}`;
}

/** Did a person hear about it? `null` while delivery is still unknown. */
export function deliveryText(e: AlertEvent): string | null {
	if (e.delivered_to === null) return null;
	if (e.delivered_to.length === 0) return 'nobody was notified';
	return `sent to ${e.delivered_to.join(', ')}`;
}

/** Recovered episodes only: the firing rows are the "now" list. */
export function resolvedEpisodes(items: AlertEvent[]): AlertEvent[] {
	return items.filter((e) => e.event === 'resolved');
}

export interface Destination {
	text: string;
	tone: 'ok' | 'warn' | 'danger';
}

/**
 * "Alerts go to: ntfy phone (ok)" / "Alerts go to no phone". The principle
 * the review put first: an alert nobody receives is not an alert, so the page
 * says in words where they go - and says so loudest when the answer is nowhere.
 */
export function destination(channels: ChannelSummary[]): Destination {
	const live = channels.filter((c) => c.enabled);
	if (live.length === 0) {
		return {
			text: 'Alerts reach this page and the alert history only - no notification channel is set up.',
			tone: 'warn',
		};
	}
	const parts = live.map((c) => `${c.name} (${c.kind}, ${STATE_WORDS[c.state]})`);
	const failing = live.some((c) => c.state === 'failing');
	const proven = live.some((c) => c.state === 'ok');
	return {
		text: `Alerts go to ${parts.join(', ')}.`,
		tone: failing ? 'danger' : proven ? 'ok' : 'warn',
	};
}

const STATE_WORDS: Record<ChannelSummary['state'], string> = {
	ok: 'last delivery ok',
	failing: 'last delivery FAILED',
	untested: 'never tested',
	disabled: 'disabled',
};
