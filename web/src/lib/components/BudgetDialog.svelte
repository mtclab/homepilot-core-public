<script lang="ts">
	// "Budget" for one friend (3.12): four axes, blank = no limit, with what the
	// friend uses now beside each field. Saving replaces the whole budget (the
	// server's semantics, unchanged); Remove budget deletes it (#607) - not the
	// same as four blanks, which keeps a budget that happens to be unlimited.
	import { createEventDispatcher, onMount, tick } from 'svelte';
	import { api, type Friend } from '$lib/api';
	import { gb } from '$lib/guests';

	export let friend: Friend;

	const dispatch = createEventDispatcher<{ close: void; saved: { cn: string } }>();
	const uid = `budget-${Math.random().toString(36).slice(2, 9)}`;

	let dialog: HTMLDialogElement | undefined;
	let first: HTMLInputElement | undefined;
	let working = false;
	let error = '';

	const l = friend.limits;
	// Memory is asked in GB and stored in MB. The field starts as the stored
	// value in GB; when it is left untouched the stored MB is sent back as it
	// was, so opening and saving never rounds a budget someone set in MB.
	const memoryStart = l?.memory_mb == null ? '' : gb(l.memory_mb);
	let values: Record<'vms' | 'cores' | 'memory' | 'disk', string | number> = {
		vms: l?.vms ?? '',
		cores: l?.cores ?? '',
		memory: memoryStart,
		disk: l?.disk_gb ?? '',
	};

	function blank(v: string | number | null | undefined): boolean {
		return v === '' || v === null || v === undefined;
	}

	function whole(name: string, v: string | number): number | null {
		if (blank(v)) return null;
		const n = Number(v);
		if (!Number.isInteger(n) || n < 0) throw new Error(`${name} must be a whole number, 0 or more, or blank for no limit.`);
		return n;
	}

	function memoryMb(v: string | number): number | null {
		if (blank(v)) return null;
		if (String(v) === memoryStart && l?.memory_mb != null) return l.memory_mb;
		const n = Number(v);
		if (!Number.isFinite(n) || n < 0) throw new Error('Memory must be 0 or more GB, or blank for no limit.');
		return Math.round(n * 1024);
	}

	async function save() {
		error = '';
		let body;
		try {
			body = {
				cn: friend.cn,
				max_vms: whole('Machines', values.vms),
				max_cores: whole('Cores', values.cores),
				max_memory_mb: memoryMb(values.memory),
				max_disk_gb: whole('Disk', values.disk),
			};
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			return;
		}
		working = true;
		try {
			await api.setGuestQuota(body);
			dispatch('saved', { cn: friend.cn });
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			working = false;
		}
	}

	async function remove() {
		error = '';
		working = true;
		try {
			await api.deleteGuestQuota(friend.cn);
			dispatch('saved', { cn: friend.cn });
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			working = false;
		}
	}

	function close() {
		if (!working) dispatch('close');
	}

	function onCancelEvent(event: Event) {
		event.preventDefault();
		close();
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.preventDefault();
			close();
		}
	}

	onMount(async () => {
		if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
		else dialog?.setAttribute('open', '');
		await tick();
		first?.focus();
	});

	$: u = friend.usage;
</script>

<dialog
	bind:this={dialog}
	class="guest-dialog card"
	aria-labelledby="{uid}-title"
	aria-describedby="{uid}-lead"
	data-testid="budget-dialog"
	on:cancel={onCancelEvent}
	on:keydown={onKeydown}
>
	<form class="section-stack" on:submit|preventDefault={save}>
		<h2 id="{uid}-title" class="text-sm font-semibold text-ink">Budget for {friend.cn}</h2>
		<p class="prose-body text-sm" id="{uid}-lead">
			The most {friend.cn} may hold across all their machines, checked when they open an invite. Blank is no limit.
		</p>

		<div class="axes">
			<label class="field-label" for="{uid}-vms">Machines</label>
			<input id="{uid}-vms" bind:this={first} class="input num" type="number" min="0" step="1" placeholder="no limit" bind:value={values.vms} aria-describedby="{uid}-vms-use {uid}-lead" />
			<span class="use" id="{uid}-vms-use">uses {u.vms}</span>

			<label class="field-label" for="{uid}-cores">Cores</label>
			<input id="{uid}-cores" class="input num" type="number" min="0" step="1" placeholder="no limit" bind:value={values.cores} aria-describedby="{uid}-cores-use {uid}-lead" />
			<span class="use" id="{uid}-cores-use">uses {u.cores}</span>

			<label class="field-label" for="{uid}-memory">Memory (GB)</label>
			<input id="{uid}-memory" class="input num" type="number" min="0" step="0.25" placeholder="no limit" bind:value={values.memory} aria-describedby="{uid}-memory-use {uid}-lead" />
			<span class="use" id="{uid}-memory-use">uses {gb(u.memory_mb)} GB</span>

			<label class="field-label" for="{uid}-disk">Disk (GB)</label>
			<input id="{uid}-disk" class="input num" type="number" min="0" step="1" placeholder="no limit" bind:value={values.disk} aria-describedby="{uid}-disk-use {uid}-lead" />
			<span class="use" id="{uid}-disk-use">uses {u.disk_gb} GB</span>
		</div>

		<p class="prose-note text-xs">Lowering a budget never touches machines that already exist; it stops the next one.</p>

		{#if error}<p class="text-sm text-danger" role="alert">{error}</p>{/if}

		<div class="end">
			{#if friend.limits}
				<button type="button" class="btn btn-ghost text-xs remove" disabled={working} on:click={remove}>Remove budget</button>
			{/if}
			<span class="grow"></span>
			<button type="button" class="btn btn-ghost text-xs" disabled={working} on:click={close}>Cancel</button>
			<button type="submit" class="btn btn-primary text-xs" disabled={working}>{working ? 'Working...' : 'Save budget'}</button>
		</div>
	</form>
</dialog>

<style>
	.guest-dialog {
		width: min(28rem, calc(100vw - 2rem));
		max-height: calc(100vh - 2rem);
		overflow-y: auto;
		padding: var(--space-5);
		color: var(--color-text);
		border-color: var(--color-border-strong);
	}
	.guest-dialog::backdrop {
		background: rgb(0 0 0 / 0.55);
	}
	.axes {
		display: grid;
		grid-template-columns: 7rem minmax(0, 7rem) minmax(0, 1fr);
		gap: var(--space-2) var(--space-3);
		align-items: center;
	}
	.axes .input {
		width: 100%;
		min-width: 0;
	}
	.use {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--color-muted);
	}
	.end {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		align-items: center;
	}
	.grow {
		flex: 1;
	}
	.remove {
		color: var(--color-danger);
	}
	@media (max-width: 767px) {
		.guest-dialog {
			padding: var(--space-4);
		}
		.axes {
			grid-template-columns: minmax(0, 1fr) minmax(0, 5.5rem) minmax(0, 4.5rem);
		}
	}
</style>
