<script lang="ts">
	// "Add a disk" on a VM's page (#824). The form PROPOSES one change -
	// attach, format, mount - and opens its review; nothing is attached until
	// the owner approves it there. What it shows is the server's answer: the
	// storages Proxmox lists for VM disks, the slot the disk would take, and
	// anything that would refuse the proposal now.
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import {
		ApiError,
		getDiskOptions,
		proposeDisk,
		type DiskOptions,
		type DiskRequest,
	} from '$lib/api';

	export let hostId: string;

	let open = false;
	let options: DiskOptions | null = null;
	let unread = '';
	let storage = '';
	let sizeGib = 20;
	let filesystem: 'ext4' | 'xfs' = 'ext4';
	let mountPoint = '/srv/data';
	let label = 'data';
	let ssd = false;
	let bus: 'scsi' | 'virtio' = 'scsi';
	let mountOptions = '';
	let optionsTouched = false;
	let reason = '';
	let busy = false;
	let refused = '';

	async function show() {
		open = true;
		refused = '';
		try {
			options = await getDiskOptions(hostId);
			unread = '';
			if (!storage && options.storages.length) {
				storage = [...options.storages].sort((a, b) => (b.avail ?? 0) - (a.avail ?? 0))[0].storage;
			}
		} catch (e) {
			unread = e instanceof Error ? e.message : String(e);
		}
	}

	$: if (options && !optionsTouched) {
		mountOptions = ssd ? options.ssd_mount_options : options.default_mount_options;
	}
	$: labelMax = options?.label_max[filesystem] ?? 16;
	$: chosen = options?.storages.find((s) => s.storage === storage);
	$: freeGib = chosen?.avail != null ? Math.floor(chosen.avail / 1024 ** 3) : null;
	$: slot = options?.next_slot[bus] ?? null;
	$: blocked = !options || options.blockers.length > 0;

	function gib(n: number | null): string {
		return n == null ? 'unknown free' : `${n} GiB free`;
	}

	async function submit() {
		busy = true;
		refused = '';
		const data: DiskRequest = {
			storage,
			size_gib: Number(sizeGib),
			filesystem,
			mount_point: mountPoint.trim(),
			label: label.trim(),
			mount_options: mountOptions.trim(),
			bus,
			ssd,
			...(reason.trim() ? { reason: reason.trim() } : {}),
		};
		try {
			const proposal = await proposeDisk(hostId, data);
			await goto(`${base}/changes/${encodeURIComponent(proposal.id)}`);
		} catch (e) {
			refused = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
</script>

<section class="card p-4 space-y-3" aria-labelledby="add-disk" id="add-disk-card" data-add-disk>
	<div class="flex items-center justify-between gap-2">
		<h2 class="section-title" id="add-disk">Add a disk</h2>
		{#if !open}
			<button type="button" class="btn btn-ghost text-xs" on:click={show}>Add a disk</button>
		{/if}
	</div>
	{#if open}
		<p class="prose-note text-sm">
			Proposes one change for you to approve: attach a new disk from a Proxmox storage, then inside
			the guest make a filesystem on it (only if it is blank), add it to /etc/fstab by label with
			<code>nofail</code>, and mount it. Nothing happens until you approve it.
		</p>
		{#if unread}
			<p class="text-danger text-sm">Could not read the guest from Proxmox: {unread}</p>
		{:else if !options}
			<p class="prose-note text-sm">Reading the guest and its storages from Proxmox…</p>
		{:else}
			<p class="text-sm" data-add-disk-facts>
				<span class="text-muted">Next slot</span>
				<span class="text-ink font-mono">{slot ?? 'none free'}</span>
				<span class="text-muted ml-3">Disk hotplug</span>
				<span class="text-ink">{options.hotplug_disk ? 'on' : 'off'}</span>
				<span class="text-muted ml-3">Guest agent</span>
				<span class="text-ink">{options.agent_answers ? 'answers' : 'not answering'}</span>
			</p>
			{#if options.blockers.length}
				<ul class="text-danger text-sm list-disc pl-5" data-add-disk-blockers>
					{#each options.blockers as b (b)}<li>{b}</li>{/each}
				</ul>
			{/if}
			<form class="grid gap-3 text-sm" on:submit|preventDefault={submit}>
				<label class="block">
					<span class="text-muted">Storage</span>
					<select class="input mt-1" bind:value={storage} required>
						{#each options.storages as s (s.storage)}
							<option value={s.storage}>{s.storage} ({s.type ?? 'storage'}, {gib(s.avail != null ? Math.floor(s.avail / 1024 ** 3) : null)})</option>
						{/each}
					</select>
				</label>
				<label class="block">
					<span class="text-muted">Size (GiB)</span>
					<input
						class="input mt-1"
						type="number"
						min={options.size_gib.min}
						max={freeGib ?? options.size_gib.max}
						bind:value={sizeGib}
						required
					/>
				</label>
				<label class="block">
					<span class="text-muted">Filesystem</span>
					<select class="input mt-1" bind:value={filesystem}>
						{#each options.filesystems as fs (fs)}<option value={fs}>{fs}</option>{/each}
					</select>
				</label>
				<label class="block">
					<span class="text-muted">Mount point</span>
					<input class="input mt-1 font-mono" bind:value={mountPoint} required autocomplete="off" />
				</label>
				<label class="block">
					<span class="text-muted">Label (up to {labelMax} characters)</span>
					<input
						class="input mt-1 font-mono"
						bind:value={label}
						maxlength={labelMax}
						pattern="[A-Za-z0-9_-]+"
						required
						autocomplete="off"
					/>
				</label>
				<label class="flex items-center gap-2">
					<input type="checkbox" bind:checked={ssd} />
					<span>This storage is on SSDs (attach and mount with discard)</span>
				</label>
				<details>
					<summary class="text-muted cursor-pointer">More options</summary>
					<div class="grid gap-3 mt-2">
						<label class="block">
							<span class="text-muted">Mount options (must keep nofail)</span>
							<input
								class="input mt-1 font-mono"
								bind:value={mountOptions}
								on:input={() => (optionsTouched = true)}
								autocomplete="off"
							/>
						</label>
						<label class="block">
							<span class="text-muted">Bus</span>
							<select class="input mt-1" bind:value={bus}>
								<option value="scsi">scsi</option>
								<option value="virtio">virtio</option>
							</select>
						</label>
						<label class="block">
							<span class="text-muted">Why (shown in the change)</span>
							<input class="input mt-1" bind:value={reason} maxlength="120" autocomplete="off" />
						</label>
					</div>
				</details>
				<div class="flex gap-2">
					<button type="submit" class="btn btn-primary text-xs" disabled={busy || blocked}>
						{busy ? 'Checking…' : 'Propose the change'}
					</button>
					<button type="button" class="btn btn-ghost text-xs" disabled={busy} on:click={() => (open = false)}>Cancel</button>
				</div>
			</form>
		{/if}
		{#if refused}
			<p class="text-danger text-sm" role="alert" data-add-disk-refused>{refused}</p>
		{/if}
	{/if}
</section>
