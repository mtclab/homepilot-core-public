<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type KBEntry, sessionStore } from '$lib/api';
	import { notify } from '$lib/stores';
	import { canWrite as capCanWrite, isAdmin as capIsAdmin } from '$lib/capabilities';
	import { groupByKind } from '$lib/grouping';

	let query = '';
	let filterKind = '';
	let items: KBEntry[] = [];
	let total = 0;
	let loading = false;
	// A load/search failure, shown inline. Never let a failure be indistinguishable
	// from an empty result (#445 B4).
	let loadError = '';
	let searched = false;

	let showForm = false;
	let formTarget = '';
	let formKind = 'note';
	let formContent = '';
	let formSupersedes = '';
	let saving = false;

	let editEntry: KBEntry | null = null;
	let editTitle = '';
	let editContent = '';
	let editKind = '';
	let editTarget = '';
	let editSaving = false;

	let deleteConfirmId: number | null = null;

	// Gate write/admin controls off the server's normalized capability list, not
	// the raw scope string (a plain `read,write` token was wrongly read-only).
	$: capabilities = $sessionStore?.capabilities;
	$: isAdmin = capIsAdmin(capabilities);
	$: canWrite = capCanWrite(capabilities);

	async function search() {
		loading = true;
		searched = true;
		loadError = '';
		try {
			if (query.trim()) {
				const res = await api.searchKB(query.trim(), filterKind || undefined, 50);
				items = res.results;
				total = res.total;
			} else {
				const res = await api.listKB({ kind: filterKind || undefined });
				items = res.items;
				total = res.total;
			}
			// A new result set invalidates whichever entry was expanded.
			openId = null;
		} catch (e) {
			// Kept on screen, NOT just toasted. Clearing items and toasting made a
			// failed search render as "No knowledge base entries yet" - a failure
			// shown as a successful empty result, which is the worst way to be
			// wrong: the operator concludes the KB is empty and stops looking.
			loadError = e instanceof Error ? e.message : String(e);
			items = [];
			total = 0;
		} finally {
			loading = false;
		}
	}

	async function createNote() {
		if (!formContent.trim()) {
			notify('Content is required', 'err');
			return;
		}
		saving = true;
		try {
			const supersedes = formSupersedes.trim()
				? formSupersedes.split(',').map((s) => s.trim()).filter(Boolean)
				: undefined;
			await api.createKBNote(formTarget.trim(), formKind, formContent.trim(), supersedes);
			notify('Note created', 'ok');
			showForm = false;
			formTarget = '';
			formKind = 'note';
			formContent = '';
			formSupersedes = '';
			await search();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			saving = false;
		}
	}

	function startEdit(entry: KBEntry) {
		editEntry = entry;
		editTitle = entry.title || '';
		editContent = entry.content || '';
		editKind = entry.kind || '';
		editTarget = entry.target || '';
	}

	async function saveEdit() {
		if (!editEntry) return;
		editSaving = true;
		try {
			const data: Record<string, string> = {};
			if (editTitle !== (editEntry.title || '')) data.title = editTitle;
			if (editContent !== (editEntry.content || '')) data.content = editContent;
			if (editKind !== (editEntry.kind || '')) data.kind = editKind;
			if (editTarget !== (editEntry.target || '')) data.target = editTarget;
			if (Object.keys(data).length > 0) {
				await api.updateKBDoc(editEntry.id, data);
				notify('Note updated', 'ok');
			}
			editEntry = null;
			await search();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			editSaving = false;
		}
	}

	async function confirmDelete(docId: number) {
		deleteConfirmId = docId;
	}

	async function doDelete(docId: number) {
		deleteConfirmId = null;
		try {
			await api.deleteKBDoc(docId);
			notify('Note deleted', 'ok');
			await search();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	$: hasActiveFilters = query.trim() !== '' || filterKind !== '';

	function clearFilters() {
		query = '';
		filterKind = '';
		search();
	}

	// Progressive disclosure: an entry is one line until asked. The KB's bodies
	// are paragraphs of prose - fifty of them stacked is the wall F5 removes.
	let openId: number | null = null;
	function toggle(id: number) {
		openId = openId === id ? null : id;
		// Collapsing the row an edit/delete was staged on must not leave the
		// confirmation armed out of sight.
		if (openId !== id) deleteConfirmId = null;
	}

	/** The one-line gist of an entry: its own title, else the first line of the body. */
	function summaryOf(entry: KBEntry): string {
		if (entry.title) return entry.title;
		const first = (entry.content || '').split('\n').find((l) => l.trim() !== '') ?? '';
		return first.length > 110 ? `${first.slice(0, 110)}…` : first;
	}

	// Kinds come off the DATA, never a hardcoded list: the KB's kind column is
	// open (ingest can write kinds the UI has never heard of) and a hardcoded set
	// would silently drop them. KNOWN_KINDS only decides the ORDER the groups
	// appear in; anything else follows alphabetically.
	const KNOWN_KINDS = ['note', 'policy', 'doc', 'fact'];
	$: kindGroups = groupByKind(items, (e) => e.kind, KNOWN_KINDS);

	function kindColor(kind: string): string {
		if (kind === 'note') return 'text-accent';
		if (kind === 'doc') return 'text-note';
		if (kind === 'fact') return 'text-ok';
		if (kind === 'policy') return 'text-warn';
		return 'text-muted';
	}

	onMount(search);
</script>

<div class="page-stack">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Knowledge Base</h1>
		<div class="flex gap-2 items-center">
			<span class="text-muted text-xs">{total} entries</span>
			{#if canWrite}
				<button class="btn btn-primary text-xs" on:click={() => (showForm = !showForm)}>
					{showForm ? 'Cancel' : '+ New Note'}
				</button>
			{/if}
		</div>
	</div>

	<!-- Search is the KB's primary action, so it sits directly under the title -
	     above the create/edit forms. It used to be pushed below a five-field
	     form, i.e. below the fold exactly when an operator had one open. -->
	<div class="flex gap-2">
		<label class="flex-1">
			<span class="sr-only">Search the knowledge base</span>
			<input
				class="input w-full text-sm"
				type="search"
				placeholder="Search KB…"
				bind:value={query}
				on:keydown={(e) => e.key === 'Enter' && search()}
			/>
		</label>
		<label>
			<span class="sr-only">Filter by kind</span>
			<select class="input text-xs" bind:value={filterKind} on:change={search}>
				<option value="">All kinds</option>
				<option value="note">note</option>
				<option value="doc">doc</option>
				<option value="fact">fact</option>
				<option value="policy">policy</option>
			</select>
		</label>
		<button class="btn btn-ghost text-xs" on:click={search}>Search</button>
	</div>

	{#if showForm}
		<form class="card p-4 space-y-3" on:submit|preventDefault={createNote}>
			<div class="flex gap-3">
				<div class="flex-1">
					<label class="field-label block mb-1">
						<span class="block mb-1">Target</span>
						<input
							class="input text-sm w-full"
							placeholder="e.g. nginx, haproxy"
							bind:value={formTarget}
						/>
					</label>
				</div>
				<div>
					<label class="field-label block mb-1">
						<span class="block mb-1">Kind</span>
						<select class="input text-sm" bind:value={formKind}>
							<option value="note">note</option>
							<option value="policy">policy</option>
							<option value="fact">fact</option>
						</select>
					</label>
				</div>
			</div>
			<div>
				<label class="field-label block mb-1">
					<span class="block mb-1">Content</span>
					<textarea
						class="input text-sm w-full font-serif"
						rows="5"
						placeholder="Enter knowledge base note content…"
						bind:value={formContent}
						required
					></textarea>
				</label>
			</div>
			<div>
				<label class="field-label block mb-1">
					<span class="block mb-1">Supersedes (comma-separated IDs, optional)</span>
					<input
						class="input text-sm w-full"
						placeholder="artifact-id-1, artifact-id-2"
						bind:value={formSupersedes}
					/>
				</label>
			</div>
			<div class="flex justify-end">
				<button class="btn btn-primary text-xs" type="submit" disabled={saving}>
					{saving ? 'Saving…' : 'Create Note'}
				</button>
			</div>
		</form>
	{/if}

	{#if editEntry}
		<form class="card p-4 space-y-3 border-accent-border" on:submit|preventDefault={saveEdit}>
			<h2 class="section-title">Edit KB Entry #{editEntry.id}</h2>
			<div class="flex gap-3">
				<div>
					<label class="field-label block mb-1">
						<span class="block mb-1">Kind</span>
						<select class="input text-sm" bind:value={editKind}>
							<option value="note">note</option>
							<option value="policy">policy</option>
							<option value="fact">fact</option>
							<option value="doc">doc</option>
						</select>
					</label>
				</div>
				<div class="flex-1">
					<label class="field-label block mb-1">
						<span class="block mb-1">Target</span>
						<input class="input text-sm w-full" bind:value={editTarget} />
					</label>
				</div>
			</div>
			<div>
				<label class="field-label block mb-1">
					<span class="block mb-1">Title</span>
					<input class="input text-sm w-full" bind:value={editTitle} />
				</label>
			</div>
			<div>
				<label class="field-label block mb-1">
					<span class="block mb-1">Content</span>
					<textarea class="input text-sm w-full font-serif" rows="6" bind:value={editContent}></textarea>
				</label>
			</div>
			<div class="flex justify-end gap-2">
				<button class="btn btn-ghost text-xs" type="button" on:click={() => (editEntry = null)}>Cancel</button>
				<button class="btn btn-primary text-xs" type="submit" disabled={editSaving}>
					{editSaving ? 'Saving…' : 'Save'}
				</button>
			</div>
		</form>
	{/if}

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-sm text-ink-strong">The knowledge base could not be searched.</p>
			<p class="prose-note text-xs">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={search}>Try again</button>
		</div>
	{:else if items.length === 0 && searched && hasActiveFilters}
		<div class="card p-6 text-center space-y-3">
			<p class="prose-note text-sm">No entries match the current search / filter.</p>
			<button class="btn btn-ghost text-xs" on:click={clearFilters}>Clear filters</button>
		</div>
	{:else if items.length === 0 && searched}
		<div class="card p-6 text-center space-y-1">
			<p class="prose-note text-sm">No knowledge base entries yet.</p>
			{#if canWrite}
				<p class="prose-note text-xs">Create the first one with “+ New Note”.</p>
			{/if}
		</div>
	{:else}
		{#each kindGroups as group (group.kind)}
			<section class="section-stack" aria-labelledby="kb-group-{group.kind}">
				<h2 class="section-title" id="kb-group-{group.kind}">
					<span class={kindColor(group.kind)}>{group.kind}</span>
					<span class="text-muted font-normal num-inline">({group.items.length})</span>
				</h2>
				<ul class="card divide-y divide-divider">
					{#each group.items as entry (entry.id)}
						<li>
							<button
								class="flex items-baseline gap-3 w-full text-left px-3 py-1.5 text-xs hover:text-ink-strong"
								aria-expanded={openId === entry.id}
								on:click={() => toggle(entry.id)}
							>
								{#if entry.target}
									<span class="text-muted font-mono whitespace-nowrap">{entry.target}</span>
								{/if}
								<span class="text-ink truncate">{summaryOf(entry)}</span>
								<span class="text-muted ml-auto pl-2" aria-hidden="true"
								>{openId === entry.id ? '▾' : '▸'}</span>
							</button>
							{#if openId === entry.id}
								<div class="px-3 pb-3 space-y-2">
									<p class="prose-body prose-measure text-xs whitespace-pre-wrap">{entry.content}</p>
									<div class="flex items-center gap-3 text-xs text-muted">
										<span>#{entry.id}</span>
										<span>{entry.source}</span>
										{#if entry.embedded_at}<span>embedded {entry.embedded_at}</span>{/if}
										<span class="ml-auto flex gap-1">
											{#if canWrite}
												<button class="btn btn-ghost text-xs" on:click={() => startEdit(entry)}>Edit</button>
											{/if}
											{#if isAdmin}
												{#if deleteConfirmId === entry.id}
													<button class="btn btn-danger text-xs" on:click={() => doDelete(entry.id)}>Confirm</button>
													<button class="btn btn-ghost text-xs" on:click={() => (deleteConfirmId = null)}>Cancel</button>
												{:else}
													<button class="btn btn-danger text-xs" on:click={() => confirmDelete(entry.id)}>Delete</button>
												{/if}
											{/if}
										</span>
									</div>
								</div>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/each}
	{/if}
</div>