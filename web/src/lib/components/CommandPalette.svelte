<script lang="ts">
	// The command palette (UI slice 2, UI plan 3.9 / 5.1). Ctrl+K (Cmd+K) from
	// anywhere, or "/" outside a text field. One dialog: a query field, the
	// scope it searches in, and the results grouped by where they came from.
	//
	// Honest by source: each remote source is asked once per query and answers
	// on its own, so a knowledge base that is down says "could not search" in
	// its own group while hosts and changes still answer.
	//
	// A real dialog for a keyboard: focus moves into the field on open, stays
	// inside the dialog (Tab switches scope rather than leaving it), Escape
	// closes, and focus goes back to whatever had it before.
	import { createEventDispatcher, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { api, ApiError } from '$lib/api';
	import { debounce } from '$lib/debounce';
	import {
		DEBOUNCE_MS,
		REMOTE_MIN_CHARS,
		SCOPES,
		SCOPE_STORAGE_KEY,
		SOURCE_LABEL,
		changeResults,
		cycleScope,
		hostResults,
		isPaletteShortcut,
		knowledgeResults,
		matchPages,
		palettePages,
		parseScope,
		sourceNote,
		sourcesFor,
		type PaletteResult,
		type PaletteScope,
		type RemoteSource,
		type SourceState,
	} from '$lib/palette';
	import Icon from './Icon.svelte';
	import Lamp from './Lamp.svelte';

	/** Open or closed; the shell binds it so its Search button can open it. */
	export let open = false;
	/** Whether the Guests noun is visible, so the palette offers what the sidebar does. */
	export let guests = false;

	const dispatch = createEventDispatcher<{ close: void }>();

	let query = '';
	let scope: PaletteScope = 'all';
	let input: HTMLInputElement | undefined;
	let returnFocus: HTMLElement | null = null;
	let activeKey = '';
	// Has the operator picked a row (arrows, pointer)? Until then the
	// selection follows the TOP row as sources answer, so a slow hosts answer
	// arriving after a quick changes answer still puts the host first under
	// Enter. Once they pick, their row stays put while it is offered.
	let picked = false;
	// Enter pressed while the sources were still answering: open the first row
	// that arrives for this same query, instead of swallowing the keystroke.
	let queuedEnter = '';

	const IDLE: SourceState = { status: 'idle' };
	let states: Record<RemoteSource, SourceState> = { hosts: IDLE, changes: IDLE, knowledge: IDLE };
	// Each query gets a number; an answer to an older query is dropped, so a slow
	// reply for "ll" can never overwrite the rows for "llm".
	let seq = 0;

	function reason(e: unknown): string {
		if (e instanceof ApiError) return e.status ? `HTTP ${e.status}` : 'no answer';
		return 'no answer';
	}

	async function ask(source: RemoteSource, q: string, n: number) {
		try {
			let items: PaletteResult[];
			if (source === 'hosts') {
				const res = await api.listInventory({ q, limit: 8, templates: 'exclude' });
				items = hostResults(res.items ?? [], q);
			} else if (source === 'changes') {
				const res = await api.listArtifacts({ q, limit: 6 });
				items = changeResults(res.items ?? []);
			} else {
				const res = await api.searchKB(q, undefined, 5);
				items = knowledgeResults(res.results ?? [], q);
			}
			if (n === seq) states = { ...states, [source]: { status: 'ok', items } };
		} catch (e) {
			if (n === seq) states = { ...states, [source]: { status: 'error', reason: reason(e) } };
		}
	}

	const searchRemote = debounce((q: string, sc: PaletteScope) => {
		const n = ++seq;
		const wanted = sourcesFor(sc);
		for (const s of wanted) void ask(s, q, n);
	}, DEBOUNCE_MS);

	/** Re-run the remote half for the current query and scope. */
	function refreshRemote() {
		picked = false;
		const q = query.trim();
		const wanted = sourcesFor(scope);
		// Sources outside the scope, and every source below the minimum, are
		// idle - not "no results": they were not asked.
		const next: Record<RemoteSource, SourceState> = { hosts: IDLE, changes: IDLE, knowledge: IDLE };
		if (q.length >= REMOTE_MIN_CHARS) for (const s of wanted) next[s] = { status: 'loading' };
		states = next;
		if (q.length >= REMOTE_MIN_CHARS && wanted.length) searchRemote(q, scope);
		else {
			searchRemote.cancel();
			seq++;
		}
	}

	$: pages = palettePages({ guests });
	$: showPages = scope === 'all' || scope === 'pages';
	// No query: the pages themselves, a jump list. A query: the matching ones.
	$: pageRows = !showPages
		? []
		: query.trim()
			? matchPages(pages, query, scope === 'pages' ? 20 : 6)
			: pages.map((p) => ({ key: `page:${p.href}`, group: 'pages' as const, label: p.label, detail: '', href: p.href }));

	interface Group {
		id: RemoteSource | 'pages';
		label: string;
		rows: PaletteResult[];
		note: string | null;
	}
	$: groups = [
		{ id: 'pages', label: SOURCE_LABEL.pages, rows: pageRows, note: null },
		...sourcesFor(scope).map((s) => {
			const st = states[s];
			return {
				id: s,
				label: SOURCE_LABEL[s],
				rows: st.status === 'ok' ? st.items : [],
				note: sourceNote(s, st),
			};
		}),
	].filter((g) => g.rows.length || g.note) as Group[];
	$: flat = groups.flatMap((g) => g.rows);
	$: busy = Object.values(states).some((s) => s.status === 'loading');
	$: q = query.trim();
	$: if (!picked || !flat.some((r) => r.key === activeKey)) activeKey = flat[0]?.key ?? '';
	$: if (queuedEnter && queuedEnter === q && flat.length) {
		queuedEnter = '';
		choose(flat[0]);
	}
	$: if (queuedEnter && queuedEnter === q && !busy && !flat.length) queuedEnter = '';
	$: hint =
		q.length > 0 && q.length < REMOTE_MIN_CHARS && sourcesFor(scope).length
			? `Type ${REMOTE_MIN_CHARS} letters to search hosts, changes and knowledge too.`
			: '';
	$: nothing =
		q.length >= REMOTE_MIN_CHARS && !busy && flat.length === 0 && !groups.some((g) => g.note)
			? `Nothing matches "${q}" in ${scopeWords(scope)}.`
			: q.length > 0 && q.length < REMOTE_MIN_CHARS && flat.length === 0
				? `No page matches "${q}".`
				: '';

	function scopeWords(sc: PaletteScope): string {
		return sc === 'all' ? 'pages, hosts, changes or knowledge' : SOURCE_LABEL[sc].toLowerCase();
	}

	function rememberScope(sc: PaletteScope) {
		try {
			localStorage.setItem(SCOPE_STORAGE_KEY, sc);
		} catch {
			// A browser with storage blocked keeps the scope for this visit only.
		}
	}

	function setScope(sc: PaletteScope) {
		scope = sc;
		rememberScope(sc);
		refreshRemote();
		input?.focus();
	}

	async function show() {
		if (open && input) {
			input.focus();
			return;
		}
		returnFocus = (document.activeElement as HTMLElement | null) ?? null;
		try {
			scope = parseScope(localStorage.getItem(SCOPE_STORAGE_KEY));
		} catch {
			scope = 'all';
		}
		query = '';
		queuedEnter = '';
		refreshRemote();
		wasOpen = true;
		open = true;
		await tick();
		input?.focus();
	}

	function close(restore = true) {
		if (!open) return;
		wasOpen = false;
		open = false;
		searchRemote.cancel();
		seq++;
		queuedEnter = '';
		dispatch('close');
		if (restore && returnFocus && document.contains(returnFocus)) returnFocus.focus();
		returnFocus = null;
	}

	function choose(r: PaletteResult | undefined) {
		if (!r) return;
		close(false);
		void goto(base + r.href);
	}

	function move(step: 1 | -1) {
		if (!flat.length) return;
		const i = flat.findIndex((r) => r.key === activeKey);
		const next = i < 0 ? 0 : (i + step + flat.length) % flat.length;
		activeKey = flat[next].key;
		picked = true;
		void tick().then(() =>
			document.getElementById(optionId(activeKey))?.scrollIntoView?.({ block: 'nearest' }),
		);
	}

	function onInputKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			move(1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			move(-1);
		} else if (e.key === 'Enter') {
			e.preventDefault();
			const r = flat.find((x) => x.key === activeKey);
			if (r) choose(r);
			else if (busy && q) queuedEnter = q;
		}
	}

	// Escape, Tab and the opening chord are handled on the window, so they
	// work wherever focus sits - including a scope button the mouse clicked.
	function onWindowKeydown(e: KeyboardEvent) {
		if (!open) {
			if (isPaletteShortcut(e)) {
				e.preventDefault();
				void show();
			}
			return;
		}
		if (e.key === 'Escape') {
			e.preventDefault();
			close();
		} else if (e.key === 'Tab') {
			// The focus trap: Tab never leaves the dialog, it changes where the
			// query looks (plan 3.9), and the field keeps the caret.
			e.preventDefault();
			setScope(cycleScope(scope, e.shiftKey ? -1 : 1));
		} else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
			e.preventDefault();
			close();
		}
	}

	function optionId(key: string): string {
		return `palette-opt-${key.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
	}

	// The shell opens it by setting `open`; route that through show() so the
	// field is focused and the previous focus remembered.
	let wasOpen = false;
	$: if (open !== wasOpen) {
		wasOpen = open;
		if (open && !input) void show();
	}
</script>

<svelte:window on:keydown={onWindowKeydown} />

{#if open}
	<!-- The backdrop is a click target for the mouse only; the keyboard closes
	     with Escape, which the dialog says in its foot. -->
	<div class="palette-backdrop" role="presentation" on:mousedown|self={() => close()} data-palette-backdrop>
		<div
			class="palette"
			role="dialog"
			aria-modal="true"
			aria-labelledby="palette-title"
			data-palette
		>
			<h2 id="palette-title" class="sr-only">Search HomePilot</h2>
			<div class="palette-head">
				<Icon name="search" size={16} class="text-muted" />
				<input
					bind:this={input}
					bind:value={query}
					on:input={refreshRemote}
					on:keydown={onInputKeydown}
					class="palette-input"
					type="text"
					role="combobox"
					aria-label="Search pages, hosts, changes and knowledge"
					aria-expanded="true"
					aria-controls="palette-list"
					aria-autocomplete="list"
					aria-activedescendant={activeKey ? optionId(activeKey) : undefined}
					autocomplete="off"
					spellcheck="false"
					placeholder="Search pages, hosts, changes, knowledge"
				/>
			</div>
			<div class="palette-scopes" role="group" aria-label="Search in">
				{#each SCOPES as s (s.id)}
					<button
						type="button"
						tabindex="-1"
						class="tag palette-scope {scope === s.id ? 'tag-accent' : ''}"
						aria-pressed={scope === s.id}
						on:click={() => setScope(s.id)}>{s.label}</button
					>
				{/each}
			</div>

			<div id="palette-list" class="palette-list" role="listbox" aria-label="Results">
				{#each groups as g (g.id)}
					<div role="group" aria-labelledby="palette-group-{g.id}" data-palette-group={g.id}>
						<div class="palette-group" id="palette-group-{g.id}">{g.label}</div>
						{#each g.rows as r (r.key)}
							<!-- Options are pointer targets; the keyboard reaches them from the
							     field through aria-activedescendant, as a combobox does. -->
							<div
								id={optionId(r.key)}
								role="option"
								tabindex="-1"
								aria-selected={r.key === activeKey}
								class="palette-option"
								data-href={r.href}
								on:mousemove={() => {
									activeKey = r.key;
									picked = true;
								}}
								on:click={() => choose(r)}
								on:keydown={(e) => e.key === 'Enter' && choose(r)}
							>
								{#if r.lamp}<Lamp state={r.lamp} />{/if}
								<span class="palette-label">{r.label}</span>
								{#if r.detail}<span class="palette-detail font-mono">{r.detail}</span>{/if}
							</div>
						{/each}
						{#if g.note}
							<div
								role="option"
								aria-selected="false"
								aria-disabled="true"
								class="palette-note {g.note.includes('could not') ? 'text-warn' : 'text-muted'}"
								data-palette-note={g.id}
							>
								{g.note}
							</div>
						{/if}
					</div>
				{/each}
				{#if nothing}
					<p class="palette-empty prose-note">{nothing}</p>
				{/if}
			</div>
			{#if hint}<p class="palette-hint">{hint}</p>{/if}
			<div class="palette-foot" aria-hidden="true">
				<span><kbd class="font-mono">Up</kbd>/<kbd class="font-mono">Down</kbd> move</span>
				<span><kbd class="font-mono">Enter</kbd> opens</span>
				<span><kbd class="font-mono">Tab</kbd> switches scope</span>
				<span><kbd class="font-mono">Esc</kbd> closes</span>
			</div>
		</div>
	</div>
{/if}
