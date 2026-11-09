// The command palette (UI slice 2, UI plan 3.9 / 5.1): any page two
// keystrokes away, any host three.
//
// This module is the pure half - which pages exist, how a query matches them,
// how each remote source's answer becomes result rows, and which keys open the
// palette - so all of it is assertable without mounting the dialog. The
// component (CommandPalette.svelte) owns the requests, focus and keyboard.
//
// Sources: the pages of the console (static, from the same models the sidebar
// and the section tabs render), hosts (`GET /inventory?q=`), changes
// (`GET /artifacts?q=`) and the knowledge base (`GET /kb/search`). Each remote
// source is asked once per query and answers on its own: one that fails says
// "could not search" in its own group and never blanks the others.

import { NAV_GROUPS, visibleNav, type NavGates } from './nav';
import { CHANGES_TABS, HOSTS_TABS, MONITORING_TABS, RECORDS_TABS } from './sections';
import { SETTINGS_TABS } from './settingsTabs';
import type { Artifact, Host, KBEntry } from './api';

/** Where a query looks. `all` asks every source; the others ask one. */
export type PaletteScope = 'all' | 'pages' | 'hosts' | 'changes' | 'knowledge';

export const SCOPES: { id: PaletteScope; label: string }[] = [
	{ id: 'all', label: 'All' },
	{ id: 'pages', label: 'Pages' },
	{ id: 'hosts', label: 'Hosts' },
	{ id: 'changes', label: 'Changes' },
	{ id: 'knowledge', label: 'Knowledge' },
];

/** The remote sources, in the order their groups are shown. */
export type RemoteSource = 'hosts' | 'changes' | 'knowledge';
export const REMOTE_SOURCES: RemoteSource[] = ['hosts', 'changes', 'knowledge'];

/** The group heading, also used in "Hosts: could not search". */
export const SOURCE_LABEL: Record<RemoteSource | 'pages', string> = {
	pages: 'Pages',
	hosts: 'Hosts',
	changes: 'Changes',
	knowledge: 'Knowledge',
};

/** A remote source is asked only from this many characters: one letter
 *  matches half the estate and costs three requests per keystroke. */
export const REMOTE_MIN_CHARS = 2;
/** The debounce between the last keystroke and the remote requests (plan 3.9). */
export const DEBOUNCE_MS = 150;

/** One row the operator can open. `href` is app-relative (no `base`). */
export interface PaletteResult {
	key: string;
	group: RemoteSource | 'pages';
	label: string;
	/** A short muted line after the label: state, kind, where it lives. */
	detail: string;
	href: string;
	/** Status lamp for a host row; absent for everything else. */
	lamp?: 'ok' | 'crit' | 'off';
}

// ---------------------------------------------------------------------------
// Pages
// ---------------------------------------------------------------------------

export interface PalettePage {
	label: string;
	href: string;
	/** Words an operator would type for this page that are not in its label. */
	keywords: string[];
}

/** Extra words per href. A page with none is found by its label alone. */
const KEYWORDS: Record<string, string[]> = {
	'/': ['home', 'dashboard', 'attention', 'start'],
	'/hosts': ['fleet', 'machines', 'vms', 'inventory'],
	'/hosts/agents': ['enrol', 'enroll', 'join', 'install agent', 'hub', 'bootstrap token'],
	'/monitoring/alerts': ['firing', 'alert history', 'deliveries', 'ntfy', 'send test'],
	'/monitoring/rules': ['alert rules', 'thresholds', 'notification channels', 'ntfy', 'webhooks'],
	'/guests': ['friends', 'invites', 'quota'],
	'/changes': ['artifacts', 'proposals'],
	'/changes/inbox': ['approve', 'proposed', 'review', 'pending', 'ready to run', 'withdraw'],
	'/changes/drift': ['in spec', 'unverified', 'disagree'],
	'/assistants': ['tokens', 'api token', 'mcp'],
	'/records/tasks': ['runs', 'failed tasks', 'jobs'],
	'/records/journal': ['audit', 'log', 'history'],
	'/records/kb': ['knowledge base', 'notes', 'docs', 'kb'],
	'/settings?tab=connection': ['api base', 'health'],
	'/settings?tab=proxmox': ['pve', 'cluster', 'proxmox token'],
	'/settings?tab=subsystems': ['self-check', 'selfcheck', 'health', 'vault', 'reconciler'],
	'/settings?tab=about': ['version', 'release'],
};


/**
 * Every page the palette offers, in sidebar order: each nav noun, then its
 * tabs. Built from the same models the sidebar and the tab bars render, so a
 * tab added to Monitoring (Capacity, GPU when their collectors land) is offered
 * here without touching this file.
 */
export function palettePages(gates: NavGates): PalettePage[] {
	const out: PalettePage[] = [];
	const push = (label: string, href: string) =>
		out.push({ label, href, keywords: KEYWORDS[href] ?? [] });
	const sub: Record<string, { label: string; href: string }[]> = {
		'/hosts': HOSTS_TABS,
		'/monitoring': MONITORING_TABS,
		'/changes': CHANGES_TABS,
		'/records': RECORDS_TABS,
		'/settings': SETTINGS_TABS.map((t) => ({ label: t.label, href: `/settings?tab=${t.id}` })),
	};
	for (const group of visibleNav(NAV_GROUPS, gates)) {
		for (const item of group.items) {
			const tabs = sub[item.href];
			// A section whose first tab IS the section (Hosts > Fleet at /hosts)
			// is offered once, under the tab's fuller name.
			if (!tabs || !tabs.some((t) => t.href === item.href)) push(item.label, item.href);
			for (const t of tabs ?? []) push(`${item.label} > ${t.label}`, t.href);
		}
	}
	return out;
}

/**
 * Pages matching `query`: every word must occur in the label or a keyword.
 * A label that starts with the query ranks first, a label containing it next,
 * a keyword-only match last; ties keep sidebar order.
 */
export function matchPages(pages: PalettePage[], query: string, limit = 6): PaletteResult[] {
	const q = query.trim().toLowerCase();
	if (!q) return [];
	const words = q.split(/\s+/);
	const scored: { page: PalettePage; score: number; i: number }[] = [];
	pages.forEach((page, i) => {
		const label = page.label.toLowerCase();
		const hay = [label, ...page.keywords.map((k) => k.toLowerCase())].join(' | ');
		if (!words.every((w) => hay.includes(w))) return;
		const lastPart = label.split(' > ').pop() ?? label;
		const score = label.startsWith(q) || lastPart.startsWith(q) ? 0 : label.includes(q) ? 1 : 2;
		scored.push({ page, score, i });
	});
	return scored
		.sort((a, b) => a.score - b.score || a.i - b.i)
		.slice(0, limit)
		.map(({ page }) => ({
			key: `page:${page.href}`,
			group: 'pages' as const,
			label: page.label,
			detail: '',
			href: page.href,
		}));
}

// ---------------------------------------------------------------------------
// Remote sources -> rows
// ---------------------------------------------------------------------------

/** Hosts, the exact hostname first, then the ones that start with the query. */
export function hostResults(hosts: Host[], query: string): PaletteResult[] {
	const q = query.trim().toLowerCase();
	const rank = (h: Host) => {
		const name = h.hostname.toLowerCase();
		return name === q ? 0 : name.startsWith(q) ? 1 : 2;
	};
	return [...hosts]
		.map((h, i) => ({ h, i }))
		.sort((a, b) => rank(a.h) - rank(b.h) || a.i - b.i)
		.map(({ h }) => {
			const facts = [
				h.status ?? 'unknown',
				h.env ?? '',
				h.proxmox_id ? `VM ${h.proxmox_id}` : '',
				h.ip_address ?? '',
			].filter(Boolean);
			return {
				key: `host:${h.id}`,
				group: 'hosts' as const,
				label: h.hostname,
				detail: facts.join(' · '),
				href: `/hosts/${encodeURIComponent(h.id)}`,
				lamp: h.status === 'online' || h.status === 'running' ? ('ok' as const) : h.status === 'offline' || h.status === 'stopped' ? ('crit' as const) : ('off' as const),
			};
		});
}

/** Changes: the artifact id is what the operator types and reads. */
export function changeResults(artifacts: Artifact[]): PaletteResult[] {
	return artifacts.map((a) => ({
		key: `change:${a.id}`,
		group: 'changes' as const,
		label: a.id,
		detail: [a.kind, a.status, a.intent].filter(Boolean).join(' · '),
		href: `/changes/${encodeURIComponent(a.id)}`,
	}));
}

/** The first line of a KB body, for an entry with no title. */
function firstLine(text: string, max = 80): string {
	const line = (text ?? '').split('\n').find((l) => l.trim()) ?? '';
	const t = line.replace(/^#+\s*/, '').trim();
	return t.length > max ? `${t.slice(0, max - 3)}...` : t;
}

/**
 * Knowledge: the KB page has no per-entry route, so the door is the KB page
 * with the same search run and this entry opened (`?q=` + `open=`).
 */
export function knowledgeResults(entries: KBEntry[], query: string): PaletteResult[] {
	return entries.map((e) => ({
		key: `kb:${e.id}`,
		group: 'knowledge' as const,
		label: firstLine(e.title ?? '') || firstLine(e.content) || `entry ${e.id}`,
		detail: [e.kind, e.target].filter(Boolean).join(' · '),
		href: `/records/kb?q=${encodeURIComponent(query.trim())}&open=${e.id}`,
	}));
}

// ---------------------------------------------------------------------------
// Per-source state, and what the operator reads about it
// ---------------------------------------------------------------------------

export type SourceState =
	| { status: 'idle' }
	| { status: 'loading' }
	| { status: 'ok'; items: PaletteResult[] }
	| { status: 'error'; reason: string };

/**
 * The one line a group shows instead of rows, or null when it shows rows (or
 * nothing at all). A failed source says so in words - an empty group would read
 * as "no host matches", which the palette has not established.
 */
export function sourceNote(source: RemoteSource, state: SourceState): string | null {
	if (state.status === 'loading') return `${SOURCE_LABEL[source]}: searching...`;
	if (state.status === 'error') return `${SOURCE_LABEL[source]}: could not search (${state.reason})`;
	return null;
}

/** The sources a scope asks. */
export function sourcesFor(scope: PaletteScope): RemoteSource[] {
	if (scope === 'all') return REMOTE_SOURCES;
	if (scope === 'pages') return [];
	return [scope];
}

/** The next scope for Tab (+1) or Shift+Tab (-1), wrapping. */
export function cycleScope(scope: PaletteScope, step: 1 | -1): PaletteScope {
	const i = SCOPES.findIndex((s) => s.id === scope);
	return SCOPES[(i + step + SCOPES.length) % SCOPES.length].id;
}

/** A remembered scope from storage, or `all` for anything that is not one. */
export function parseScope(raw: string | null | undefined): PaletteScope {
	return SCOPES.some((s) => s.id === raw) ? (raw as PaletteScope) : 'all';
}

export const SCOPE_STORAGE_KEY = 'hp.palette.scope';

// ---------------------------------------------------------------------------
// Keys
// ---------------------------------------------------------------------------

/** True for a field that takes typed text - "/" there is a slash, not a command. */
export function isEditable(target: EventTarget | null): boolean {
	if (!target || typeof (target as HTMLElement).tagName !== 'string') return false;
	const el = target as HTMLElement;
	if (el.isContentEditable) return true;
	const tag = el.tagName.toLowerCase();
	if (tag === 'textarea' || tag === 'select') return true;
	if (tag !== 'input') return false;
	const type = ((el as HTMLInputElement).type || 'text').toLowerCase();
	return !['button', 'checkbox', 'radio', 'submit', 'reset', 'range', 'color', 'file'].includes(type);
}

/**
 * Does this keydown open the palette? Ctrl+K (Cmd+K on a Mac) from anywhere -
 * it is the one chord an input has no use for - and "/" only outside a text
 * field, with no modifier, so typing a path into a form is never hijacked.
 */
export function isPaletteShortcut(e: Pick<KeyboardEvent, 'key' | 'ctrlKey' | 'metaKey' | 'altKey' | 'shiftKey' | 'target'>): boolean {
	if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'k') return true;
	return e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !isEditable(e.target);
}
