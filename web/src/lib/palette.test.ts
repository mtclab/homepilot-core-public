import { fireEvent, render, screen, waitFor, within, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
// test-mocks MUST come before the components, or they resolve the real $lib/api.
import { goto, setPage } from './test-mocks';
import { api, ApiError } from '$lib/api';
import { CHANGES_TABS, MONITORING_TABS, RECORDS_TABS } from '$lib/sections';
import {
	REMOTE_MIN_CHARS,
	cycleScope,
	hostResults,
	isPaletteShortcut,
	knowledgeResults,
	matchPages,
	palettePages,
	parseScope,
	sourceNote,
} from './palette';
import Layout from '../routes/+layout.svelte';
import CommandPalette from './components/CommandPalette.svelte';
import KBPage from '../routes/records/kb/+page.svelte';
import type { Host, KBEntry } from './api';

/**
 * THE PALETTE GATE (UI slice 2, UI plan 3.9 / 5.1): any page two keystrokes
 * away, any host three - asserted as the operator's journey, not as "a dialog
 * rendered": from the shell, Ctrl+K, type a host's name, Enter, and the host's
 * page is where the app goes.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - drop the window keydown handler and the Ctrl+K and "/" journeys fail;
 *   - let "/" fire inside a text field and the "never hijacks typing" case fails;
 *   - treat a failed source as an empty one and the "could not search" case
 *     fails (the group would vanish, reading as "no knowledge matches");
 *   - stop restoring focus on Escape and the focus case fails;
 *   - point a page entry at a route that does not exist and the reach case fails;
 *   - drop the sequence guard and the stale-answer case shows the old rows.
 */

const listInventory = api.listInventory as ReturnType<typeof vi.fn>;
const listArtifacts = api.listArtifacts as ReturnType<typeof vi.fn>;
const searchKB = api.searchKB as ReturnType<typeof vi.fn>;

const LLM: Host = { id: 'h-llm', hostname: 'llm', status: 'online', env: 'prod', proxmox_id: 101 };
const LLM_TEST: Host = { id: 'h-llmt', hostname: 'llm-test', status: 'stopped', env: 'dev' };

const ROUTES = resolve(process.cwd(), 'src', 'routes');

/** The route file an app-relative href renders (query and hash ignored). */
function routeFile(href: string): string {
	const path = href.split(/[?#]/)[0].replace(/^\//, '');
	return resolve(ROUTES, path, '+page.svelte');
}

/** Wait past the palette's debounce for the remote sources to be asked. */
async function settle() {
	await new Promise((r) => setTimeout(r, 200));
}

describe('palette: pages', () => {
	it('offers every sidebar noun and every section tab, each a real route', () => {
		const pages = palettePages({ guests: true });
		const labels = pages.map((p) => p.label);
		for (const l of [
			'Overview',
			'Hosts > Fleet',
			'Hosts > Agents',
			'Monitoring > Alerts',
			'Monitoring > Rules',
			'Guests',
			'Changes > Inbox',
			'Changes > All',
			'Changes > Drift',
			'Assistants',
			'Records > Knowledge base',
			'Settings > Subsystems',
		]) {
			expect(labels, l).toContain(l);
		}
		for (const p of pages) {
			expect(existsSync(routeFile(p.href)), `${p.label} -> ${p.href} has no route`).toBe(true);
		}
	});

	it('offers the Changes and Records tabs the section layouts render, never a retired page', () => {
		// 3.10.0 integration: the palette kept its own copy of the Changes
		// tabs and still offered "Changes > Review queue" after the Inbox
		// replaced it. The route still exists (as a redirect for old links), so
		// "is a real route" passed. Now both read sections.ts, and an offered
		// page must render something of its own, not only forward elsewhere.
		const pages = palettePages({ guests: false });
		const offered = pages.map((p) => p.href);
		for (const t of [...CHANGES_TABS, ...RECORDS_TABS]) expect(offered).toContain(t.href);
		// A section noun may forward to its first tab (Monitoring -> Alerts, as
		// the sidebar does); a "Section > Tab" entry is a view of its own.
		for (const p of pages.filter((x) => x.label.includes(' > '))) {
			const src = readFileSync(routeFile(p.href), 'utf8');
			expect(src, `${p.label} -> ${p.href} only redirects`).not.toMatch(/^\s*<Redirect\b/m);
		}
		expect(matchPages(pages, 'approve')[0].href).toBe('/changes/inbox');
		expect(matchPages(pages, 'ready to run')[0].label).toBe('Changes > Inbox');
	});

	it('grows with the Monitoring tabs, so a GPU tab is offered the day it lands', () => {
		const offered = palettePages({ guests: false }).map((p) => p.href);
		for (const t of MONITORING_TABS) expect(offered).toContain(t.href);
	});

	it('hides Guests where the sidebar does', () => {
		expect(palettePages({ guests: false }).some((p) => p.href === '/guests')).toBe(false);
	});

	it('matches a label first, then a keyword, every word required', () => {
		const pages = palettePages({ guests: false });
		expect(matchPages(pages, 'rules')[0].label).toBe('Monitoring > Rules');
		expect(matchPages(pages, 'ntfy').map((r) => r.label)).toEqual(['Monitoring > Alerts', 'Monitoring > Rules']);
		expect(matchPages(pages, 'enrol')[0].href).toBe('/hosts/agents');
		expect(matchPages(pages, 'drift')[0].href).toBe('/changes/drift');
		expect(matchPages(pages, 'monitoring alerts').map((r) => r.label)).toEqual(['Monitoring > Alerts']);
		expect(matchPages(pages, 'zzz')).toEqual([]);
	});
});

describe('palette: rows and keys', () => {
	it('puts the exact hostname first, then the ones that start with it', () => {
		const rows = hostResults([{ ...LLM_TEST }, { id: 'x', hostname: 'old-llm' }, LLM], 'llm');
		expect(rows.map((r) => r.label)).toEqual(['llm', 'llm-test', 'old-llm']);
		expect(rows[0].href).toBe('/hosts/h-llm');
		expect(rows[0].detail).toBe('online · prod · VM 101');
		expect(rows[0].lamp).toBe('ok');
		expect(rows[1].lamp).toBe('crit');
	});

	it('doors a knowledge hit to the KB page with the same search and that entry open', () => {
		const e: KBEntry = { id: 7, source: 's', kind: 'note', target: 'llm', content: '# GPU map\nbody' };
		const [row] = knowledgeResults([e], 'llm gpu');
		expect(row.label).toBe('GPU map');
		expect(row.href).toBe('/records/kb?q=llm%20gpu&open=7');
	});

	it('says a failed source could not search, in words, and is silent while idle', () => {
		expect(sourceNote('knowledge', { status: 'error', reason: 'HTTP 503' })).toBe(
			'Knowledge: could not search (HTTP 503)',
		);
		expect(sourceNote('hosts', { status: 'idle' })).toBeNull();
		expect(sourceNote('hosts', { status: 'ok', items: [] })).toBeNull();
	});

	it('opens on Ctrl+K or Cmd+K anywhere, and on "/" only outside a text field', () => {
		const input = document.createElement('input');
		const textarea = document.createElement('textarea');
		const checkbox = Object.assign(document.createElement('input'), { type: 'checkbox' });
		const base = { ctrlKey: false, metaKey: false, altKey: false, shiftKey: false };
		expect(isPaletteShortcut({ ...base, key: 'k', ctrlKey: true, target: input })).toBe(true);
		expect(isPaletteShortcut({ ...base, key: 'K', metaKey: true, target: document.body })).toBe(true);
		expect(isPaletteShortcut({ ...base, key: '/', target: document.body })).toBe(true);
		expect(isPaletteShortcut({ ...base, key: '/', target: checkbox })).toBe(true);
		expect(isPaletteShortcut({ ...base, key: '/', target: input })).toBe(false);
		expect(isPaletteShortcut({ ...base, key: '/', target: textarea })).toBe(false);
		expect(isPaletteShortcut({ ...base, key: 'k', target: document.body })).toBe(false);
	});

	it('cycles scope with Tab both ways, and forgets a stored scope it does not know', () => {
		expect(cycleScope('all', 1)).toBe('pages');
		expect(cycleScope('all', -1)).toBe('knowledge');
		expect(cycleScope('knowledge', 1)).toBe('all');
		expect(parseScope('hosts')).toBe('hosts');
		expect(parseScope('<script>')).toBe('all');
		expect(parseScope(null)).toBe('all');
	});
});

describe('palette: the journey from the shell', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/records/tasks');
		try {
			localStorage.clear();
		} catch {
			/* storage-less browser */
		}
		listInventory.mockResolvedValue({ items: [], total: 0 });
		listArtifacts.mockResolvedValue({ items: [], total: 0 });
		searchKB.mockResolvedValue({ results: [], total: 0 });
	});
	afterEach(() => cleanup());

	async function openWithCtrlK(): Promise<HTMLInputElement> {
		render(Layout);
		await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
		const dialog = await screen.findByRole('dialog', { name: 'Search HomePilot' });
		const input = within(dialog).getByRole('combobox') as HTMLInputElement;
		await waitFor(() => expect(document.activeElement).toBe(input));
		return input;
	}

	it('Ctrl+K, "llm", Enter: the llm host page opens', async () => {
		listInventory.mockImplementation(async (p: { q?: string }) =>
			p.q === 'llm' ? { items: [LLM_TEST, LLM], total: 2 } : { items: [], total: 0 },
		);
		const input = await openWithCtrlK();
		await fireEvent.input(input, { target: { value: 'llm' } });
		await settle();
		const option = await screen.findByRole('option', { name: /^llm online/ });
		expect(option).toHaveAttribute('aria-selected', 'true');
		expect(input.getAttribute('aria-activedescendant')).toBe(option.id);
		expect(listInventory).toHaveBeenCalledWith({ q: 'llm', limit: 8, templates: 'exclude' });

		await fireEvent.keyDown(input, { key: 'Enter' });
		expect(goto).toHaveBeenCalledWith('/ui/hosts/h-llm');
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	// Found on the real build: Changes answered before Hosts, the first row
	// that arrived was preselected and kept, and Enter opened a KB note's
	// change instead of the host. The selection follows the top row until the
	// operator picks one.
	it('opens the host even when Changes answers first', async () => {
		let releaseHosts: (v: unknown) => void = () => {};
		listInventory.mockImplementation(() => new Promise((r) => (releaseHosts = r)));
		listArtifacts.mockResolvedValue({
			items: [{ id: 'kb-note-llm', status: 'applied', kind: 'kb-note', intent: 'llm box GPU map', created_at: '' }],
			total: 1,
		});
		const input = await openWithCtrlK();
		await fireEvent.input(input, { target: { value: 'llm' } });
		await settle();
		expect(await screen.findByRole('option', { name: /^kb-note-llm/ })).toHaveAttribute('aria-selected', 'true');
		releaseHosts({ items: [LLM], total: 1 });
		const host = await screen.findByRole('option', { name: /^llm online/ });
		await waitFor(() => expect(host).toHaveAttribute('aria-selected', 'true'));
		await fireEvent.keyDown(input, { key: 'Enter' });
		expect(goto).toHaveBeenCalledWith('/ui/hosts/h-llm');
	});

	it('keeps the row the operator picked when a later source answers', async () => {
		let releaseHosts: (v: unknown) => void = () => {};
		listInventory.mockImplementation(() => new Promise((r) => (releaseHosts = r)));
		listArtifacts.mockResolvedValue({
			items: [
				{ id: 'llm-a', status: 'applied', kind: 'kb-note', intent: '', created_at: '' },
				{ id: 'llm-b', status: 'applied', kind: 'kb-note', intent: '', created_at: '' },
			],
			total: 2,
		});
		const input = await openWithCtrlK();
		await fireEvent.input(input, { target: { value: 'llm' } });
		await settle();
		await screen.findByRole('option', { name: /^llm-b/ });
		await fireEvent.keyDown(input, { key: 'ArrowDown' });
		releaseHosts({ items: [LLM], total: 1 });
		await screen.findByRole('option', { name: /^llm online/ });
		expect(screen.getByRole('option', { name: /^llm-b/ })).toHaveAttribute('aria-selected', 'true');
	});

	it('moves with the arrows and opens the row it is on', async () => {
		listInventory.mockResolvedValue({ items: [LLM, LLM_TEST], total: 2 });
		const input = await openWithCtrlK();
		await fireEvent.input(input, { target: { value: 'llm' } });
		await settle();
		await screen.findByRole('option', { name: /^llm-test/ });
		await fireEvent.keyDown(input, { key: 'ArrowDown' });
		expect(screen.getByRole('option', { name: /^llm-test/ })).toHaveAttribute('aria-selected', 'true');
		await fireEvent.keyDown(input, { key: 'Enter' });
		expect(goto).toHaveBeenCalledWith('/ui/hosts/h-llmt');
	});

	it('finds a page by a word in it: "rules" opens Monitoring > Rules', async () => {
		const input = await openWithCtrlK();
		await fireEvent.input(input, { target: { value: 'rules' } });
		const first = screen.getAllByRole('option')[0];
		expect(first.textContent).toContain('Monitoring > Rules');
		await fireEvent.keyDown(input, { key: 'Enter' });
		expect(goto).toHaveBeenCalledWith('/ui/monitoring/rules');
	});

	it('with the knowledge base failing, its group says "could not search" and the others still answer', async () => {
		listInventory.mockResolvedValue({ items: [LLM], total: 1 });
		listArtifacts.mockResolvedValue({
			items: [{ id: 'llm-gpu-split', status: 'proposed', kind: 'set_resources', intent: 'give llm two GPUs', created_at: '' }],
			total: 1,
		});
		searchKB.mockRejectedValue(new ApiError(503, '{"detail":"embedding service down"}'));
		const input = await openWithCtrlK();
		await fireEvent.input(input, { target: { value: 'llm' } });
		await settle();

		const note = await screen.findByText('Knowledge: could not search (HTTP 503)');
		expect(note).toHaveAttribute('aria-disabled', 'true');
		expect(screen.getByRole('option', { name: /^llm online/ })).toBeInTheDocument();
		expect(screen.getByRole('option', { name: /^llm-gpu-split/ })).toBeInTheDocument();
		// No sentence claims nothing matched: the palette has not established that.
		expect(screen.queryByText(/Nothing matches/)).toBeNull();
	});

	it('asks no remote source for one letter, and says how many it needs', async () => {
		const input = await openWithCtrlK();
		await fireEvent.input(input, { target: { value: 'l' } });
		await settle();
		expect(listInventory).not.toHaveBeenCalled();
		expect(screen.getByText(`Type ${REMOTE_MIN_CHARS} letters to search hosts, changes and knowledge too.`)).toBeInTheDocument();
	});

	it('drops an answer to an older query, so "ll" can never overwrite "llm"', async () => {
		let releaseOld: (v: unknown) => void = () => {};
		listInventory.mockImplementation((p: { q?: string }) =>
			p.q === 'll'
				? new Promise((r) => (releaseOld = r))
				: Promise.resolve({ items: [LLM], total: 1 }),
		);
		const input = await openWithCtrlK();
		await fireEvent.input(input, { target: { value: 'll' } });
		await settle();
		await fireEvent.input(input, { target: { value: 'llm' } });
		await settle();
		await screen.findByRole('option', { name: /^llm online/ });
		releaseOld({ items: [{ id: 'stale', hostname: 'llama-old' }], total: 1 });
		await settle();
		expect(screen.queryByRole('option', { name: /llama-old/ })).toBeNull();
	});

	it('Tab switches scope without leaving the dialog, and only that source is asked', async () => {
		const input = await openWithCtrlK();
		await fireEvent.keyDown(window, { key: 'Tab' });
		await fireEvent.keyDown(window, { key: 'Tab' });
		const hosts = screen.getByRole('button', { name: 'Hosts' });
		expect(hosts).toHaveAttribute('aria-pressed', 'true');
		expect(document.activeElement).toBe(input);
		await fireEvent.input(input, { target: { value: 'llm' } });
		await settle();
		expect(listInventory).toHaveBeenCalled();
		expect(listArtifacts).not.toHaveBeenCalled();
		expect(searchKB).not.toHaveBeenCalled();
		// The scope is remembered per browser.
		expect(localStorage.getItem('hp.palette.scope')).toBe('hosts');
	});

	it('Escape closes and gives focus back to what had it', async () => {
		render(Layout);
		const trigger = document.querySelector('.side-search') as HTMLButtonElement;
		trigger.focus();
		await fireEvent.click(trigger);
		const dialog = await screen.findByRole('dialog', { name: 'Search HomePilot' });
		await waitFor(() => expect(document.activeElement).toBe(within(dialog).getByRole('combobox')));
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(document.activeElement).toBe(trigger);
	});

	it('"/" opens it from the page, but never while typing in a field', async () => {
		render(Layout);
		const field = document.createElement('input');
		document.body.appendChild(field);
		field.focus();
		await fireEvent.keyDown(field, { key: '/' });
		expect(screen.queryByRole('dialog')).toBeNull();
		field.remove();

		await fireEvent.keyDown(document.body, { key: '/' });
		expect(await screen.findByRole('dialog', { name: 'Search HomePilot' })).toBeInTheDocument();
	});
});

describe('palette: the knowledge door lands on the entry', () => {
	beforeEach(() => vi.clearAllMocks());

	it('the KB page runs ?q= and opens ?open=', async () => {
		searchKB.mockResolvedValue({
			results: [
				{ id: 3, source: 'a', kind: 'note', content: 'other note' },
				{ id: 7, source: 'b', kind: 'note', title: 'llm box GPU map', content: 'four cards, one missing' },
			],
			total: 2,
		});
		setPage('/ui/records/kb?q=gpu&open=7');
		render(KBPage);
		await waitFor(() => expect(searchKB).toHaveBeenCalledWith('gpu', undefined, 50));
		expect(await screen.findByText('four cards, one missing')).toBeInTheDocument();
		expect((screen.getByRole('searchbox', { name: /search the knowledge base/i }) as HTMLInputElement).value).toBe('gpu');
	});

	it('mounts standalone as well (the shell is not required to use it)', async () => {
		listInventory.mockResolvedValue({ items: [LLM], total: 1 });
		render(CommandPalette, { props: { open: true } });
		const input = await screen.findByRole('combobox');
		await fireEvent.input(input, { target: { value: 'llm' } });
		await settle();
		expect(await screen.findByRole('option', { name: /^llm online/ })).toBeInTheDocument();
	});
});
