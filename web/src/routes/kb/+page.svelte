<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type KBEntry, sessionStore } from '$lib/api';
	import { notify } from '$lib/stores';
	import { canWrite as capCanWrite, isAdmin as capIsAdmin } from '$lib/capabilities';

	let query = '';
	let filterKind = '';
	let items: KBEntry[] = [];
	let total = 0;
	let loading = false;
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
		} catch (e) {
			notify(String(e), 'err');
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

	function kindColor(kind: string): string {
		if (kind === 'note') return 'text-accent';
		if (kind === 'doc') return 'text-note';
		if (kind === 'fact') return 'text-ok';
		if (kind === 'policy') return 'text-warn';
		return 'text-muted';
	}

	onMount(search);
</script>

<div class="space-y-4">
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

	{#if showForm}
		<form class="card p-4 space-y-3" on:submit|preventDefault={createNote}>
			<div class="flex gap-3">
				<div class="flex-1">
					<label class="field-label block mb-1">Target</label>
					<input
						class="input text-sm w-full"
						placeholder="e.g. nginx, haproxy"
						bind:value={formTarget}
					/>
				</div>
				<div>
					<label class="field-label block mb-1">Kind</label>
					<select class="input text-sm" bind:value={formKind}>
						<option value="note">note</option>
						<option value="policy">policy</option>
						<option value="fact">fact</option>
					</select>
				</div>
			</div>
			<div>
				<label class="field-label block mb-1">Content</label>
				<textarea
					class="input text-sm w-full font-serif"
					rows="5"
					placeholder="Enter knowledge base note content…"
					bind:value={formContent}
					required
				></textarea>
			</div>
			<div>
				<label class="field-label block mb-1">Supersedes (comma-separated IDs, optional)</label>
				<input
					class="input text-sm w-full"
					placeholder="artifact-id-1, artifact-id-2"
					bind:value={formSupersedes}
				/>
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
					<label class="field-label block mb-1">Kind</label>
					<select class="input text-sm" bind:value={editKind}>
						<option value="note">note</option>
						<option value="policy">policy</option>
						<option value="fact">fact</option>
						<option value="doc">doc</option>
					</select>
				</div>
				<div class="flex-1">
					<label class="field-label block mb-1">Target</label>
					<input class="input text-sm w-full" bind:value={editTarget} />
				</div>
			</div>
			<div>
				<label class="field-label block mb-1">Title</label>
				<input class="input text-sm w-full" bind:value={editTitle} />
			</div>
			<div>
				<label class="field-label block mb-1">Content</label>
				<textarea class="input text-sm w-full font-serif" rows="6" bind:value={editContent}></textarea>
			</div>
			<div class="flex justify-end gap-2">
				<button class="btn btn-ghost text-xs" type="button" on:click={() => (editEntry = null)}>Cancel</button>
				<button class="btn btn-primary text-xs" type="submit" disabled={editSaving}>
					{editSaving ? 'Saving…' : 'Save'}
				</button>
			</div>
		</form>
	{/if}

	<div class="flex gap-2">
		<input
			class="input flex-1 text-sm"
			placeholder="Search KB…"
			bind:value={query}
			on:keydown={(e) => e.key === 'Enter' && search()}
		/>
		<select class="input text-xs" bind:value={filterKind} on:change={search}>
			<option value="">All kinds</option>
			<option value="note">note</option>
			<option value="doc">doc</option>
			<option value="fact">fact</option>
			<option value="policy">policy</option>
		</select>
		<button class="btn btn-ghost text-xs" on:click={search}>Search</button>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
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
		<div class="space-y-2">
			{#each items as entry}
				<div class="card p-4 space-y-1">
					<div class="flex items-center gap-3">
						<span class="text-xs font-medium {kindColor(entry.kind)}">{entry.kind}</span>
						{#if entry.target}
							<span class="text-xs text-muted font-mono">{entry.target}</span>
						{/if}
						{#if entry.title}
							<span class="text-sm text-ink font-medium">{entry.title}</span>
						{/if}
						<div class="ml-auto flex gap-1">
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
						</div>
					</div>
					<p class="prose-body text-xs line-clamp-3 whitespace-pre-wrap">{entry.content}</p>
					<div class="text-xs text-muted">{entry.source}</div>
				</div>
			{/each}
		</div>
	{/if}
</div>