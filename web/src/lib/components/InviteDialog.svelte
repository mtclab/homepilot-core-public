<script lang="ts">
	// "Invite a friend" (3.12): who, how big, how long - and the one-time link,
	// shown ONCE, in this dialog and nowhere else.
	//
	// The link is a credential that builds a machine. It lives in this
	// component's own variable from the mint response until the dialog closes:
	// never in a store, the URL, localStorage or a log, and the page's later
	// reads carry only its prefix (the server keeps a prefix and a hash).
	//
	// The size is a preset (from the server's read: what the mint route
	// accepts) or Custom. The line under it is the budget check redemption will
	// make (`wouldRefuse`, the client copy of check_provision). HomePilot has
	// no read of a node's free capacity, so the dialog says nothing about the
	// node rather than guessing.
	import { createEventDispatcher, onMount, tick } from 'svelte';
	import { base } from '$app/paths';
	import { api, type Friend, type GuestFriends, type InviteSize } from '$lib/api';
	import { budgetCheck, gb, sizeLine } from '$lib/guests';
	import { exactTime, inWords } from '$lib/relativeTime';

	export let friends: Friend[] = [];
	export let sizing: GuestFriends['sizing'];
	export let defaults: GuestFriends['defaults'];
	export let portalOrigin: string | null = null;
	/** Pre-filled certificate name (the row's Invite); empty from the page button. */
	export let cn = '';

	const dispatch = createEventDispatcher<{ close: void; minted: { id: string; cn: string } }>();
	const uid = `invite-${Math.random().toString(36).slice(2, 9)}`;

	let dialog: HTMLDialogElement | undefined;
	let cnInput: HTMLInputElement | undefined;
	let linkInput: HTMLTextAreaElement | undefined;

	const presets = sizing.presets;
	let choice: string = presets[0]?.name ?? 'custom';
	let custom = { cores: presets[0]?.cores ?? 1, memory_gb: (presets[0]?.memory_mb ?? 1024) / 1024, disk_gb: presets[0]?.disk_gb ?? 20 };
	const ttlChoices = [1, 7, 30].filter((d) => d >= sizing.ttl_days.min && d <= sizing.ttl_days.max);
	let ttlDays = ttlChoices.includes(sizing.ttl_days.default) ? sizing.ttl_days.default : ttlChoices[0];

	const noDefaults = !defaults.node || !defaults.template_vmid;
	let moreOpen = noDefaults;
	let templateVmid: string | number = '';
	let node = '';

	let working = false;
	let error = '';

	// After a successful mint: the link, once.
	let minted: { id: string; cn: string; token: string; node: string | null; template: number | null; size: InviteSize } | null = null;
	let copyState: 'idle' | 'copied' | 'selected' = 'idle';

	function presetSize(name: string): InviteSize | null {
		const p = presets.find((x) => x.name === name);
		return p ? { cores: p.cores, memory_mb: p.memory_mb, disk_gb: p.disk_gb } : null;
	}

	$: size = presetSize(choice) ?? {
		cores: Number(custom.cores),
		memory_mb: Math.round(Number(custom.memory_gb) * 1024),
		disk_gb: Number(custom.disk_gb),
	};
	$: friend = friends.find((f) => f.cn === cn.trim()) ?? null;
	$: check = budgetCheck(friend, cn, size);
	// The invite as the server recorded it (its disk may have been raised to the
	// template's), once the page has re-read the friends after the mint.
	$: recorded = minted ? friends.flatMap((f) => f.invites).find((i) => i.id === minted?.id) ?? null : null;
	$: link = minted ? `${portalOrigin ?? ''}/invite/${minted.token}` : '';
	$: builds = minted
		? [
				`Builds ${sizeLine(recorded?.caps ?? minted.size)}`,
				minted.template ? `from template ${minted.template}` : '',
				minted.node ? `on node ${minted.node}` : '',
			]
				.filter(Boolean)
				.join(' ') + '.'
		: '';

	function pick(name: string) {
		if (name === 'custom' && choice !== 'custom') {
			const from = presetSize(choice);
			if (from) custom = { cores: from.cores, memory_gb: from.memory_mb / 1024, disk_gb: from.disk_gb };
		}
		choice = name;
	}

	function label(name: string): string {
		return name.charAt(0).toUpperCase() + name.slice(1);
	}

	function outOfBounds(s: InviteSize): string {
		const b = sizing.bounds;
		const bad: string[] = [];
		if (!Number.isInteger(s.cores) || s.cores < b.cores.min || s.cores > b.cores.max)
			bad.push(`cores ${b.cores.min} to ${b.cores.max}`);
		if (!Number.isFinite(s.memory_mb) || s.memory_mb < b.memory_mb.min || s.memory_mb > b.memory_mb.max)
			bad.push(`memory ${gb(b.memory_mb.min)} to ${gb(b.memory_mb.max)} GB`);
		if (!Number.isInteger(s.disk_gb) || s.disk_gb < b.disk_gb.min || s.disk_gb > b.disk_gb.max)
			bad.push(`disk ${b.disk_gb.min} to ${b.disk_gb.max} GB`);
		return bad.length ? `A machine can have ${bad.join(', ')}.` : '';
	}

	async function submit() {
		error = '';
		const name = cn.trim();
		if (!name) {
			error = 'Type the name on their client certificate.';
			cnInput?.focus();
			return;
		}
		const bounds = outOfBounds(size);
		if (bounds) {
			error = bounds;
			return;
		}
		const tpl = templateVmid === '' || templateVmid == null ? null : Number(templateVmid);
		if ((!defaults.template_vmid && !tpl) || (!defaults.node && !node.trim())) {
			moreOpen = true;
			error = 'This instance has no provisioning default for the template or node: name them under More options.';
			return;
		}
		working = true;
		try {
			const res = await api.mintGuestInvite({
				cn: name,
				template_vmid: tpl,
				node: node.trim() || null,
				cores: size.cores,
				memory_mb: size.memory_mb,
				disk_gb: size.disk_gb,
				ttl_days: ttlDays,
			});
			minted = {
				id: res.id,
				cn: res.cn,
				token: res.token,
				node: res.caps?.node ?? null,
				template: res.caps?.template_vmid ?? null,
				size: { ...size },
			};
			dispatch('minted', { id: res.id, cn: res.cn });
			await tick();
			linkInput?.focus();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			working = false;
		}
	}

	async function copy() {
		// In the click handler itself: browsers only allow clipboard writes in a
		// user gesture. Anywhere it is refused (no permission, plain http), the
		// link is selected so one keystroke copies it.
		try {
			if (!navigator.clipboard?.writeText) throw new Error('no clipboard');
			await navigator.clipboard.writeText(link);
			copyState = 'copied';
		} catch {
			linkInput?.focus();
			linkInput?.select();
			copyState = 'selected';
		}
	}

	function close() {
		if (working) return;
		// Dropping the component drops the token with it.
		minted = null;
		dispatch('close');
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
		cnInput?.focus();
		if (cn) cnInput?.select();
	});
</script>

<dialog
	bind:this={dialog}
	class="guest-dialog card"
	aria-labelledby="{uid}-title"
	data-testid="invite-dialog"
	on:cancel={onCancelEvent}
	on:keydown={onKeydown}
>
	{#if !minted}
		<form class="section-stack" on:submit|preventDefault={submit}>
			<h2 id="{uid}-title" class="text-sm font-semibold text-ink">Invite a friend</h2>
			<p class="prose-body text-sm">They get a one-time link. Opening it builds one machine of this size for them.</p>

			<div class="field">
				<label class="field-label" for="{uid}-cn">Certificate name</label>
				<input
					id="{uid}-cn"
					bind:this={cnInput}
					bind:value={cn}
					class="input font-mono"
					autocomplete="off"
					spellcheck="false"
					placeholder="friend.example"
					aria-describedby="{uid}-cn-hint"
				/>
				<span class="hint" id="{uid}-cn-hint">The name on their client certificate, exactly as issued. Only that certificate can use the link.</span>
			</div>

			<div class="field">
				<span class="field-label" id="{uid}-size-label">Machine size</span>
				<div class="sizes" role="group" aria-labelledby="{uid}-size-label" aria-describedby="{uid}-fit">
					{#each presets as p (p.name)}
						<button type="button" class="btn" aria-pressed={choice === p.name} on:click={() => pick(p.name)}
							>{label(p.name)} · {sizeLine(p)}</button
						>
					{/each}
					<button type="button" class="btn" aria-pressed={choice === 'custom'} on:click={() => pick('custom')}>Custom</button>
				</div>
				{#if check.text}
					<span class="hint" class:refuse={!check.fits} id="{uid}-fit" data-budget-check={check.fits ? 'fits' : 'refused'}>{check.text}</span>
				{/if}
			</div>

			{#if choice === 'custom'}
				<div class="custom">
					<div class="field">
						<label class="field-label" for="{uid}-cores">Cores</label>
						<input id="{uid}-cores" class="input num" type="number" min={sizing.bounds.cores.min} max={sizing.bounds.cores.max} step="1" bind:value={custom.cores} aria-describedby="{uid}-cores-hint" />
						<span class="hint" id="{uid}-cores-hint">For this one machine, {sizing.bounds.cores.min} to {sizing.bounds.cores.max}. The friend's total is their budget.</span>
					</div>
					<div class="field">
						<label class="field-label" for="{uid}-memory">Memory (GB)</label>
						<input id="{uid}-memory" class="input num" type="number" min="0.25" step="0.25" bind:value={custom.memory_gb} aria-describedby="{uid}-memory-hint" />
						<span class="hint" id="{uid}-memory-hint">At least {gb(sizing.bounds.memory_mb.min)} GB, at most {gb(sizing.bounds.memory_mb.max)} GB.</span>
					</div>
					<div class="field">
						<label class="field-label" for="{uid}-disk">Disk (GB)</label>
						<input id="{uid}-disk" class="input num" type="number" min={sizing.bounds.disk_gb.min} max={sizing.bounds.disk_gb.max} step="1" bind:value={custom.disk_gb} aria-describedby="{uid}-disk-hint" />
						<span class="hint" id="{uid}-disk-hint">Raised to the template's own disk if that is larger: a disk cannot shrink.</span>
					</div>
				</div>
			{/if}

			<div class="field">
				<label class="field-label" for="{uid}-ttl">Link valid for</label>
				<select id="{uid}-ttl" class="input" bind:value={ttlDays} aria-describedby="{uid}-ttl-hint">
					{#each ttlChoices as d (d)}
						<option value={d}>{d} {d === 1 ? 'day' : 'days'}</option>
					{/each}
				</select>
				<span class="hint" id="{uid}-ttl-hint">After that the link stops working. It works once either way.</span>
			</div>

			<div>
				<button type="button" class="link" aria-expanded={moreOpen} aria-controls="{uid}-more" on:click={() => (moreOpen = !moreOpen)}
					>{moreOpen ? 'Fewer options' : 'More options (template, node)'}</button
				>
			</div>
			{#if moreOpen}
				<div class="custom" id="{uid}-more">
					<div class="field">
						<label class="field-label" for="{uid}-template">Template VMID</label>
						<input id="{uid}-template" class="input num" type="number" min="100" placeholder={defaults.template_vmid ? `${defaults.template_vmid} (default)` : 'required'} bind:value={templateVmid} aria-describedby="{uid}-template-hint" />
						<span class="hint" id="{uid}-template-hint">
							{#if defaults.template_vmid}Leave it empty for template {defaults.template_vmid}, this instance's provisioning default.{:else}No provisioning default is set: name the template to clone, or set a default in <a class="text-accent hover:underline" href="{base}/settings?tab=subsystems">Settings &gt; Subsystems</a>.{/if}
						</span>
					</div>
					<div class="field">
						<label class="field-label" for="{uid}-node">Node</label>
						<input id="{uid}-node" class="input font-mono" placeholder={defaults.node ? `${defaults.node} (default)` : 'required'} bind:value={node} aria-describedby="{uid}-node-hint" />
						<span class="hint" id="{uid}-node-hint">
							{#if defaults.node}Leave it empty for node {defaults.node}, this instance's provisioning default.{:else}No provisioning default is set: name the Proxmox node that builds it.{/if}
						</span>
					</div>
				</div>
			{/if}

			{#if error}<p class="text-sm text-danger" role="alert">{error}</p>{/if}

			<div class="end">
				<button type="button" class="btn btn-ghost text-xs" disabled={working} on:click={close}>Cancel</button>
				<button type="submit" class="btn btn-primary text-xs" disabled={working}>{working ? 'Working...' : 'Create invite link'}</button>
			</div>
		</form>
	{:else}
		<div class="section-stack" data-invite-link-panel>
			<h2 id="{uid}-title" class="text-sm font-semibold text-ink">Invite link for {minted.cn}</h2>
			<div class="link-row">
				<textarea
					bind:this={linkInput}
					class="input font-mono link-text"
					readonly
					rows="3"
					value={link}
					aria-label="Invite link for {minted.cn}"
					aria-describedby="{uid}-once"
					on:focus={(e) => e.currentTarget.select()}
				></textarea>
				<button type="button" class="btn btn-primary text-xs" on:click={copy}>{copyState === 'copied' ? 'Copied' : 'Copy link'}</button>
			</div>
			{#if copyState === 'selected'}
				<p class="text-xs text-warn" role="status">This browser did not allow copying: the link is selected, press Ctrl+C (Cmd+C on a Mac).</p>
			{/if}
			<p class="prose-body text-sm" id="{uid}-once">
				This is the only time the link is shown. HomePilot keeps only a prefix and a hash of it: if it is lost, revoke the invite and make another.
			</p>
			<p class="prose-note text-xs" data-minted-caps>
				{builds}
				{#if recorded}The link expires <time datetime={recorded.expires_at} title={exactTime(recorded.expires_at)}>{inWords(recorded.expires_at)}</time>.{/if}
			</p>
			{#if !portalOrigin}
				<p class="prose-note text-xs">This is the path only: put your guest portal's address in front of it (set HP_PORTAL_BASE_URL to have the whole link shown here).</p>
			{/if}
			<div class="end">
				<button type="button" class="btn text-xs" on:click={close}>Done</button>
			</div>
		</div>
	{/if}
</dialog>

<style>
	.guest-dialog {
		width: min(32rem, calc(100vw - 2rem));
		max-height: calc(100vh - 2rem);
		overflow-y: auto;
		padding: var(--space-5);
		color: var(--color-text);
		border-color: var(--color-border-strong);
	}
	.guest-dialog::backdrop {
		background: rgb(0 0 0 / 0.55);
	}
	.field {
		display: grid;
		grid-template-columns: 8rem minmax(0, 1fr);
		gap: 0.25rem var(--space-3);
		align-items: start;
	}
	.field > .field-label {
		padding-top: 0.4375rem;
	}
	.field .input {
		width: 100%;
		min-width: 0;
	}
	.field .input.num {
		max-width: 8rem;
	}
	.hint {
		grid-column: 2;
		font-size: 0.71875rem;
		color: var(--color-muted);
	}
	.hint.refuse {
		color: var(--color-warn);
	}
	.sizes {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}
	.sizes .btn[aria-pressed='true'] {
		background: var(--color-accent-tint);
		border-color: var(--color-accent-border);
		color: var(--color-text-strong);
	}
	.custom {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		padding-left: var(--space-3);
		border-left: 2px solid var(--color-divider);
	}
	.link {
		font-family: var(--font-sans);
		font-size: 0.75rem;
		color: var(--color-accent);
		background: none;
		border: 0;
		padding: 0;
		cursor: pointer;
	}
	.link:hover {
		text-decoration: underline;
	}
	.link-row {
		display: flex;
		gap: var(--space-2);
	}
	.link-row .input {
		flex: 1;
		min-width: 0;
	}
	.link-text {
		resize: none;
		word-break: break-all;
		line-height: 1.4;
	}
	.end {
		display: flex;
		justify-content: flex-end;
		gap: var(--space-2);
	}
	@media (max-width: 767px) {
		.guest-dialog {
			padding: var(--space-4);
		}
		.field {
			grid-template-columns: minmax(0, 1fr);
		}
		.hint {
			grid-column: 1;
		}
		.link-row {
			flex-direction: column;
		}
	}
</style>
