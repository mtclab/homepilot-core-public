<script lang="ts">
	import { onMount } from 'svelte';
	import {
		api,
		sessionStore,
		type GuestPowerState,
		type Host,
		type PowerAction,
		type SelfGuestStatus,
	} from '$lib/api';
	import { canWrite as capCanWrite, isAdmin as capIsAdmin } from '$lib/capabilities';
	import { ENVS, envClass, envSourceLabel } from '$lib/fleet';
	import { notify } from '$lib/stores';
	import RowMenu from '$lib/components/RowMenu.svelte';
	import HostRowMenu from '$lib/components/HostRowMenu.svelte';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import {
		VERB_LABEL,
		outcomeLine,
		refusalLine,
		workingLine,
		type LineTone,
	} from '$lib/powerMenu';
	import Icon from '$lib/components/Icon.svelte';
	import Lamp from '$lib/components/Lamp.svelte';
	import { pruneSelection } from '$lib/selection';
	import { addressCell } from '$lib/addresses';
	import { debounce } from '$lib/debounce';
	import { base } from '$app/paths';
	import {
		attentionReason,
		groupHosts,
		initialCollapsed,
		isCollapsible,
		writeCollapsePreference,
		type HostGroupId,
	} from '$lib/hostGroups';

	let items: Host[] = [];
	// The list is paged. It used to cap at 100 rows with `total` reporting the
	// PAGE SIZE, so an estate with more hosts than that simply had no page 2
	// (#428).
	let total = 0;
	let page = 0;
	const PAGE_SIZE = 100;
	let loading = true;
	let loadError = '';
	let syncing = false;
	let enriching = false;
	let filterRole = '';
	let filterStatus = '';
	let filterSource = '';
	let filterImportState = '';
	// Which part of the estate (lane B1): "which VMs are dev" is one filter.
	let filterEnv = '';
	// Proxmox templates are provisioning inputs, not fleet members: hidden
	// unless asked for, and the number hidden is always on screen.
	let showTemplates = false;
	let templatesHidden = 0;
	// Searched on the SERVER: the list is paginated, so a filter applied in the
	// browser could only ever search the page already on screen (#445 A4).
	let search = '';

	// The role vocabulary the BACKEND uses: `node` and `guest` are what the sync
	// writes, and the rest are what `ROLE_PATTERNS` in inventory/service.py can
	// infer. One list, used by both the filter and the add form, so the UI cannot
	// offer a role nothing will ever match (#424).
	const ROLES = [
		'node',
		'guest',
		'database',
		'web-server',
		'api-server',
		'monitoring',
		'control-plane',
		'worker',
	];
	let selectedIds: Set<string> = new Set();
	// Who is knocking on the agent hub (A3). The panels themselves moved to
	// Hosts > Agents (UI slice 1); the fleet list keeps ONE line naming how
	// many are waiting, with the door, so the table is not pushed down on
	// every visit when nobody is. `null` = not read yet; a failed read says
	// so rather than going quiet.
	let joining: { waiting: number; refused: number } | null = null;
	let joiningUnread = false;
	function joiningSentence(j: { waiting: number; refused: number }): string {
		const parts: string[] = [];
		if (j.waiting > 0) parts.push(`${j.waiting} host${j.waiting === 1 ? '' : 's'} waiting to join`);
		if (j.refused > 0) parts.push(`${j.refused} refused`);
		return parts.join(', ');
	}
	async function loadJoining() {
		try {
			const r = await api.getEnrolmentRequests();
			joining = { waiting: r.summary.wants_to_join, refused: r.summary.refused };
			joiningUnread = false;
		} catch {
			joining = null;
			joiningUnread = true;
		}
	}
	// Sync / Adopt / Ignore / Enrich are all write-scoped server-side. Offering
	// them to a read-only session only produces 403s. Default-deny while loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	// Moving the self-guest mark is admin-only server-side (a later stop button
	// is guarded by it), so only an admin is offered Confirm.
	$: canAdmin = capIsAdmin($sessionStore?.capabilities);

	// ── Grouping (#549 F3): attention first, then managed, then discovered.
	// The rows are the same rows and the same selection; what changes is that
	// the page leads with the machines that want an operator instead of with
	// whatever the server happened to return first.
	//
	// A row an operator just powered stays in the group it was in until they
	// reload (plan 4.7: rows never move under the cursor on a live update) -
	// a stopped guest started from "Needs attention" keeps its place, now
	// reading `running`, instead of jumping into "Managed" mid-glance.
	let pinnedGroups: Record<string, HostGroupId> = {};
	$: groups = groupHosts(items, pinnedGroups);
	// Collapse state is decided ONCE per fleet-size change, not on every render,
	// or an operator's click to open the healthy group would be undone by the
	// next reload. `collapseKey` is what makes that "once".
	let collapsed: Record<string, boolean> = {};
	let collapseKey = '';
	$: if (!loading) {
		const key = String(items.length);
		if (key !== collapseKey) {
			collapseKey = key;
			collapsed = {
				managed: initialCollapsed('managed', items.length),
				attention: false,
				discovered: false,
			};
		}
	}

	function toggleGroup(id: HostGroupId) {
		const next = !collapsed[id];
		collapsed = { ...collapsed, [id]: next };
		writeCollapsePreference(id, next);
	}

	// ── Hosts by role (#549 F3): moved off Overview, where it was a donut in a
	// three-card grid. Here it is one inline line, and each role is a filter -
	// the breakdown answers "how many web servers" AND opens them.
	//
	// The counts come from the dashboard summary because they are FLEET-wide:
	// counting `items` would silently describe page one of a paged list.
	let roleCounts: Array<[string, number]> = [];
	// The same line by env (lane B1): "which VMs are dev" answered and opened
	// in one click. `unset` is counted too - an estate half-labelled says so.
	let envCounts: Array<[string, number]> = [];

	function countsOf(by: Record<string, number> | undefined): Array<[string, number]> {
		return Object.entries(by ?? {})
			.filter(([, n]) => n > 0)
			.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
	}

	async function loadRoleCounts() {
		try {
			const d = await api.getDashboard();
			roleCounts = countsOf(d?.inventory?.by_role);
			envCounts = countsOf(d?.inventory?.by_env);
		} catch {
			// A breakdown is context, not the page. Losing it must not blank the fleet.
			roleCounts = [];
			envCounts = [];
		}
	}

	function filterToRole(role: string) {
		filterRole = filterRole === role ? '' : role;
		page = 0;
		void load();
	}

	function filterToEnv(env: string) {
		filterEnv = filterEnv === env ? '' : env;
		page = 0;
		void load();
	}

	// ── The self-guest (lane B1): HomePilot proposes which row is the machine
	// it runs on; an operator confirms once. Nothing is marked on a guess.
	let selfGuest: SelfGuestStatus | null = null;
	let selfWorking = false;

	async function loadSelfGuest() {
		try {
			selfGuest = await api.getSelfGuest();
		} catch {
			// The proposal is a convenience; the fleet stands without it.
			selfGuest = null;
		}
	}

	async function answerSelfGuest(confirm: boolean) {
		const proposal = selfGuest?.proposal;
		if (!proposal) return;
		selfWorking = true;
		try {
			selfGuest = confirm
				? await api.confirmSelfGuest(proposal.host_id)
				: await api.dismissSelfGuest(proposal.host_id);
			notify(
				confirm
					? `${proposal.hostname} is marked as the host HomePilot runs on`
					: `HomePilot will not propose ${proposal.hostname} again`,
			);
			if (confirm) await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			selfWorking = false;
		}
	}

	// Forgetting is irreversible (services and the observation note go with the
	// host), so it is a two-step confirm inline (in the row menu).
	let forgetting: string | null = null;

	async function forgetOne(h: Host) {
		forgetting = h.id;
		try {
			await api.forgetHost(h.id);
			notify(`Removed ${h.hostname} from inventory`);
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			forgetting = null;
		}
	}

	function fmtDate(s: string | null | undefined): string {
		if (!s) return '—';
		try {
			return new Date(s).toLocaleDateString();
		} catch {
			return s;
		}
	}

	// Adding a host by hand: the only way a non-Proxmox machine can enter
	// inventory at all (#445 A5).
	let showAdd = false;
	let adding = false;
	let addForm = { hostname: '', ip_address: '', role: 'guest', description: '' };

	async function submitAdd() {
		adding = true;
		try {
			const host = await api.addHost({
				hostname: addForm.hostname.trim(),
				ip_address: addForm.ip_address.trim() || undefined,
				role: addForm.role,
				description: addForm.description.trim() || undefined,
			});
			notify(`Added ${host.hostname}`);
			showAdd = false;
			addForm = { hostname: '', ip_address: '', role: 'guest', description: '' };
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			adding = false;
		}
	}

	// One request per pause in typing, not one per keystroke - the query goes to
	// the server.
	const searchAgain = debounce(() => {
		page = 0;
		load();
	}, 300);

	function firstPage() {
		page = 0;
		return load();
	}

	// ── Power from the row (UI slice 5, plan 3.2 / 5.4). What the row says
	// about an action: while it runs, "Starting... following Proxmox task"
	// with the OLD chip kept and marked pending; then the settled outcome the
	// server judged once the task ended, and the chip re-read from the server.
	// A refusal names its reason and the holder, in the row, until dismissed.
	interface PowerLine {
		phase: 'working' | 'done' | 'refused';
		action: PowerAction;
		tone: LineTone;
		text: string;
	}
	let powerLines: Record<string, PowerLine> = {};
	// The typed dialog (plan 3.10) for a forced stop, and for any verb on the
	// machine HomePilot runs on or a protected guest.
	let typedAsk: { host: Host; action: PowerAction; state: GuestPowerState } | null = null;

	function setLine(id: string, line: PowerLine | null) {
		const next = { ...powerLines };
		if (line) next[id] = line;
		else delete next[id];
		powerLines = next;
	}

	function onPowerAsk(h: Host, detail: { action: PowerAction; state: GuestPowerState; typed: boolean }) {
		if (detail.typed) typedAsk = { host: h, action: detail.action, state: detail.state };
		else void runPower(h, detail.action);
	}

	async function runPower(h: Host, action: PowerAction, confirmName?: string) {
		// Pinned where it is until the operator reloads (see pinnedGroups).
		const g = groups.find((x) => x.hosts.some((y) => y.id === h.id));
		if (g) pinnedGroups = { ...pinnedGroups, [h.id]: g.id };
		setLine(h.id, { phase: 'working', action, tone: 'muted', text: workingLine(action) });
		let line: PowerLine;
		try {
			const result = await api.powerGuest(h.id, {
				action,
				...(confirmName ? { confirm_name: confirmName } : {}),
			});
			const o = outcomeLine(result);
			line = { phase: 'done', action, tone: o.tone, text: o.text };
		} catch (e) {
			line = { phase: 'refused', action, tone: 'crit', text: refusalLine(action, e) };
		}
		// The chip changes only on the server's word: re-read the row (the
		// server wrote Proxmox's answer onto it once the task settled).
		try {
			const fresh = await api.getHost(h.id);
			if (fresh) {
				items = items.map((x) =>
					x.id === h.id
						? {
								...x,
								status: fresh.status,
								pve_status: fresh.pve_status,
								absent_since: fresh.absent_since,
							}
						: x,
				);
			}
		} catch {
			line = { ...line, text: `${line.text} (Could not re-read the row; Reload to see its state.)` };
		}
		setLine(h.id, line);
	}

	async function confirmTyped(typed: string) {
		if (!typedAsk) return;
		const { host, action } = typedAsk;
		// The dialog closes as the action starts: the row carries the progress.
		typedAsk = null;
		await runPower(host, action, typed);
	}

	// The chip that rides with the name. For a Proxmox guest it is the
	// hypervisor's own word (running / stopped / paused) - "online" needed an
	// address, so a running guest without one read "unknown".
	function stateChip(h: Host): { text: string; cls: string } {
		const guest = h.host_type === 'qemu' || h.host_type === 'lxc';
		if (guest && h.pve_status && !h.absent_since) {
			if (h.pve_status === 'running') return { text: 'running', cls: 'bg-ok-tint text-ok' };
			return { text: h.pve_status, cls: 'bg-raised text-muted' };
		}
		return { text: h.status ?? 'unknown', cls: statusChip(h.status) };
	}

	// Phones get a card list (plan 3.2 mobile); the table stays from 768 px up.
	let narrow = false;
	onMount(() => {
		const mq = typeof window !== 'undefined' ? window.matchMedia?.('(max-width: 767px)') : undefined;
		if (!mq) return;
		narrow = mq.matches;
		const onChange = (e: MediaQueryListEvent) => (narrow = e.matches);
		mq.addEventListener?.('change', onChange);
		return () => mq.removeEventListener?.('change', onChange);
	});

	async function load() {
		loading = true;
		loadError = '';
		pinnedGroups = {};
		void loadJoining();
		try {
			const res = await api.listInventory({
				role: filterRole || undefined,
				status: filterStatus || undefined,
				source: filterSource || undefined,
				import_state: filterImportState || undefined,
				env: filterEnv || undefined,
				templates: showTemplates ? 'include' : 'exclude',
				q: search.trim() || undefined,
				limit: PAGE_SIZE,
				offset: page * PAGE_SIZE,
			});
			items = res.items;
			total = res.total;
			templatesHidden = res.templates_hidden ?? 0;
			// The selection must never outlive the rows it was made on: a filter
			// change used to leave off-screen hosts selected, so "3 selected →
			// Adopt" acted on hosts the user could not see.
			selectedIds = pruneSelection(selectedIds, items);
		} catch (e) {
			loadError = e instanceof Error ? e.message : String(e);
			notify(loadError, 'err');
		} finally {
			loading = false;
		}
	}

	async function sync() {
		syncing = true;
		try {
			const res = await api.refreshInventory();
			// A sweep that did not see the whole hypervisor is not a small sync,
			// it is an unfinished one - and reported green, its counts read as
			// the estate rather than as the part of it that answered (#648
			// tranche 8). Missing `complete` means an older backend that cannot
			// say, which is not the same as saying yes.
			if (res.complete === true) {
				notify(`Synced: ${res.hosts} hosts, ${res.services} services`, 'ok');
			} else {
				notify(
					`Sync did not finish: ${res.error ?? 'the hypervisor sweep was incomplete'}. ` +
						`${res.hosts} hosts were read, so this is part of the estate, not all of it.`,
					'err',
				);
			}
			await enrich();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			syncing = false;
		}
	}

	async function enrich() {
		enriching = true;
		try {
			const res = await api.enrichInventory();
			// `enriched` counts hosts that actually CHANGED, so an all-quiet pass
			// reports zero rather than the row count.
			const unchanged = res.unchanged === undefined ? '' : `, unchanged ${res.unchanged}`;
			notify(
				`Enriched ${res.enriched}, failed ${res.failed}, skipped ${res.skipped}${unchanged}`,
				'ok',
			);
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			enriching = false;
		}
	}

	$: hasActiveFilters =
		filterRole !== '' ||
		filterStatus !== '' ||
		filterSource !== '' ||
		filterImportState !== '' ||
		filterEnv !== '' ||
		search.trim() !== '';

	function clearFilters() {
		filterRole = '';
		filterStatus = '';
		filterSource = '';
		filterImportState = '';
		filterEnv = '';
		search = '';
		page = 0;
		load();
	}

	// The state chip that rides with the hostname in the primary column. It used
	// to be a bare coloured word in a Status column of its own; the state belongs
	// WITH the thing it is a state of (#549 F1, principle 4).
	function statusChip(s?: string): string {
		if (s === 'online') return 'bg-ok-tint text-ok';
		if (s === 'offline') return 'bg-danger-tint text-danger';
		return 'bg-raised text-muted';
	}

	function sourceBadge(source?: string): string {
		if (source === 'hp_created') return 'HP';
		if (source === 'imported') return 'Imported';
		if (source === 'discovered') return 'Discovered';
		if (source === 'agent') return 'Agent';
		// A hand-added host painted an empty pill (UI slice 5 walk).
		if (source === 'manual') return 'Manual';
		return '';
	}

	function sourceClass(source?: string): string {
		if (source === 'hp_created') return 'bg-accent-tint text-accent-strong';
		if (source === 'imported') return 'bg-ok-tint text-ok';
		if (source === 'discovered') return 'bg-warn-tint text-warn';
		if (source === 'agent') return 'bg-ok-tint text-ok';
		return 'bg-raised text-ink';
	}

	function toggleSelect(id: string) {
		const next = new Set(selectedIds);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selectedIds = next;
	}

	function toggleSelectAll() {
		if (selectedIds.size === items.length) {
			selectedIds = new Set();
		} else {
			selectedIds = new Set(items.map((h) => h.id));
		}
	}

	// ── Agent fleet ops (#514 S4): the Agents tab folded into Hosts. Same
	// mechanics as S3 built them - one dialog for the batch, optimistic
	// in-place updates, no full reload.
	let agentBatch: { kind: 'forget' | 'revoke'; targets: Host[] } | null = null;
	let agentWorking = false;

	function selectDisconnectedAgents() {
		selectedIds = new Set(
			items.filter((h) => h.agent_id && !h.agent_connected).map((h) => h.id)
		);
	}

	$: selectedHosts = items.filter((h) => selectedIds.has(h.id));
	// Forget needs no live channel (#415); revoke needs one.
	$: agentForgettable = selectedHosts.filter((h) => h.agent_id && !h.agent_connected);
	$: agentRevokable = selectedHosts.filter((h) => h.agent_id && h.agent_connected);

	async function runAgentBatch() {
		if (!agentBatch) return;
		const { kind, targets } = agentBatch;
		agentWorking = true;
		const results = await Promise.allSettled(
			targets.map((h) =>
				kind === 'forget' ? api.forgetAgent(h.agent_id!) : api.revokeAgent(h.agent_id!)
			)
		);
		const failed: Host[] = [];
		results.forEach((r, i) => {
			if (r.status === 'rejected') failed.push(targets[i]);
		});
		const doneIds = new Set(
			targets.filter((t) => !failed.includes(t)).map((t) => t.id)
		);
		// In place: the host row stays (the machine exists); its agent state flips.
		items = items.map((h) =>
			doneIds.has(h.id)
				? kind === 'forget'
					? { ...h, agent_id: null, agent_connected: undefined, agent_version: null }
					: { ...h, agent_connected: false }
				: h
		);
		if (failed.length) {
			notify(
				`${failed.length} of ${targets.length} failed: ${failed.map((f) => f.hostname).join(', ')}`,
				'err'
			);
		} else {
			notify(
				kind === 'forget'
					? `Forgot ${targets.length} agent${targets.length === 1 ? '' : 's'} — credentials revoked`
					: `Revoked ${targets.length} agent${targets.length === 1 ? '' : 's'}`
			);
		}
		agentWorking = false;
		agentBatch = null;
		selectedIds = new Set();
	}

	async function bulk(action: string) {
		// Belt and braces: act only on rows that are on screen right now, whatever
		// happened to `items` since the selection was made.
		const ids = Array.from(pruneSelection(selectedIds, items));
		if (ids.length === 0) return;
		try {
			const res = await api.bulkInventory(action, ids);
			notify(`${action}: ${res.succeeded} succeeded, ${res.failed} failed`, 'ok');
			selectedIds = new Set();
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function adoptOne(host: Host) {
		try {
			await api.adoptHost(host.id);
			notify('Adopted', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	async function ignoreOne(host: Host) {
		try {
			await api.ignoreHost(host.id);
			notify('Ignored', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		}
	}

	onMount(async () => {
		await load();
		await Promise.all([loadRoleCounts(), loadSelfGuest()]);
	});
</script>

<div class="space-y-4">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<h1 class="page-title">Hosts</h1>
		<div class="flex flex-wrap gap-2">
			<button class="btn btn-ghost text-xs" on:click={load} disabled={loading}><Icon name="refresh" />Reload</button>
			{#if canWrite}
				<!-- `+ Add` (plan 3.2): the three doors a machine comes in by. -->
				<RowMenu label="Add to the fleet" triggerClass="btn btn-primary text-xs" let:close>
					<span slot="trigger" class="inline-flex items-center gap-1"
						><Icon name="plus" />Add<Icon name="chevron-down" size={12} /></span
					>
					<button
						type="button"
						role="menuitem"
						class="btn btn-ghost btn-xs text-left"
						on:click={() => {
							close();
							showAdd = true;
						}}>Add a LAN device</button
					>
					<!-- No operator provision form exists yet (review 02/11): the item
					     stays, disabled, and says where provisioning is today. -->
					<button
						type="button"
						role="menuitem"
						class="btn btn-ghost btn-xs text-left add-later"
						disabled
						aria-label="Provision a VM from template (not built yet)"
						><span>Provision a VM from template</span><span class="menu-why"
							>Not built yet. Today a guest invite or an assistant's provision_guest does it.</span
						></button
					>
					<a role="menuitem" class="btn btn-ghost btn-xs text-left" href="{base}/hosts/agents" on:click={close}
						>Enrol an agent</a
					>
				</RowMenu>
				<button class="btn btn-ghost text-xs" on:click={sync} disabled={syncing}>
					{#if syncing}Syncing…{:else}<Icon name="refresh" />Sync from Proxmox{/if}
				</button>
			{/if}
		</div>
	</div>

	<!-- Hosts by role (#549 F3). It was a donut in Overview's three-card grid,
	     where it answered a question nobody asks at a glance; on the fleet page
	     it is one line, and every role is a live filter into the table below. -->
	{#if roleCounts.length}
		<p class="text-xs text-muted flex flex-wrap items-baseline gap-x-s-2 gap-y-1">
			<span>Hosts by role</span>
			{#each roleCounts as [role, n] (role)}
				<button
					type="button"
					class="hover:text-ink {filterRole === role ? 'text-accent' : ''}"
					aria-pressed={filterRole === role}
					title={filterRole === role ? `Clear the ${role} filter` : `Show only ${role} hosts`}
					on:click={() => filterToRole(role)}>{role} <span class="num-inline text-ink">{n}</span></button>
			{/each}
		</p>
	{/if}
	{#if envCounts.length}
		<p class="text-xs text-muted flex flex-wrap items-baseline gap-x-s-2 gap-y-1">
			<span>Hosts by env</span>
			{#each envCounts as [env, n] (env)}
				{#if env === 'unset'}
					<span title="No env set here or by an hp-env-* tag in Proxmox">unset <span class="num-inline text-ink">{n}</span></span>
				{:else}
					<button
						type="button"
						class="hover:text-ink {filterEnv === env ? 'text-accent' : ''}"
						aria-pressed={filterEnv === env}
						title={filterEnv === env ? `Clear the ${env} filter` : `Show only ${env} hosts`}
						on:click={() => filterToEnv(env)}>{env} <span class="num-inline text-ink">{n}</span></button>
				{/if}
			{/each}
		</p>
	{/if}

	{#if selfGuest?.proposal && !selfGuest.confirmed}
		<!-- HomePilot proposes, the operator confirms (review 02 Q3): an address
		     the control plane answers on can be a proxy's, so nothing is marked
		     on a match alone. -->
		<div class="card section-stack" role="region" aria-label="HomePilot's own host">
			<p class="text-sm text-ink">
				HomePilot looks like it runs on
				<a class="text-accent font-mono" href="{base}/hosts/{selfGuest.proposal.host_id}"
					>{selfGuest.proposal.hostname}</a
				>{#if selfGuest.proposal.proxmox_id != null}&nbsp;(VMID {selfGuest.proposal.proxmox_id}){/if}.
			</p>
			<p class="prose-note text-xs">
				Why: {selfGuest.proposal.evidence}. Once confirmed, the row is marked and later power
				actions treat it as the machine HomePilot would go down with.
			</p>
			{#if canAdmin}
				<div class="flex gap-2">
					<button class="btn btn-primary text-xs" disabled={selfWorking}
						on:click={() => answerSelfGuest(true)}>Yes, this is HomePilot's host</button>
					<button class="btn btn-ghost text-xs" disabled={selfWorking}
						on:click={() => answerSelfGuest(false)}>Not this one</button>
				</div>
			{:else}
				<p class="prose-note text-xs">An admin can confirm it.</p>
			{/if}
		</div>
	{/if}

	{#if showAdd}
		<!-- Inventory could only be filled by a Proxmox sync, so the NAS, the
		     router and the Pi were literally unrepresentable (#445 A5). -->
		<form class="card space-y-3" on:submit|preventDefault={submitAdd}>
			<div class="grid gap-3 sm:grid-cols-2">
				<label class="space-y-1">
					<span class="text-xs text-muted">Hostname</span>
					<input
						class="input w-full text-sm"
						bind:value={addForm.hostname}
						placeholder="nas01"
						required
					/>
				</label>
				<label class="space-y-1">
					<span class="text-xs text-muted">IP address (optional)</span>
					<input class="input w-full text-sm" bind:value={addForm.ip_address} placeholder="10.0.0.4" />
				</label>
				<label class="space-y-1">
					<span class="text-xs text-muted">Role</span>
					<select class="input w-full text-sm" bind:value={addForm.role}>
						{#each ROLES as role}<option value={role}>{role}</option>{/each}
					</select>
				</label>
				<label class="space-y-1">
					<span class="text-xs text-muted">Description (optional)</span>
					<input
						class="input w-full text-sm"
						bind:value={addForm.description}
						placeholder="Synology NAS in the cupboard"
					/>
				</label>
			</div>
			<p class="prose-note text-xs">
				Added by hand, so it is recorded as a manual host and adopted straight away - and a
				Proxmox sync will never mark it gone, because Proxmox never looked for it.
			</p>
			<div class="flex gap-2">
				<button class="btn btn-primary text-xs" type="submit" disabled={adding || !addForm.hostname.trim()}>
					{adding ? 'Adding…' : 'Add host'}
				</button>
				<button class="btn btn-ghost text-xs" type="button" disabled={adding} on:click={() => (showAdd = false)}
					>Cancel</button
				>
			</div>
		</form>
	{/if}

	<div class="flex gap-3 flex-wrap">
		<label class="flex items-center gap-2">
			<span class="sr-only">Search hosts</span>
			<input
				class="input text-xs w-64"
				type="search"
				placeholder="Search name, address, role, tags…"
				bind:value={search}
				on:input={searchAgain}
			/>
		</label>
		<select class="input text-xs" bind:value={filterRole} on:change={firstPage}>
			<!-- Exactly the roles the code writes (#424). It used to offer
			     hypervisor / vm / container / service - none of which anything
			     ever writes - while omitting `node` and `guest`, the two that
			     actually exist. Filtering by them returned nothing, forever. -->
			<option value="">All roles</option>
			{#each ROLES as role}<option value={role}>{role}</option>{/each}
		</select>
		<select class="input text-xs" bind:value={filterStatus} on:change={firstPage}>
			<option value="">All statuses</option>
			<option value="online">online</option>
			<option value="offline">offline</option>
			<option value="unknown">unknown</option>
		</select>
		<select class="input text-xs" bind:value={filterSource} on:change={firstPage}>
			<option value="">All sources</option>
			<option value="hp_created">HP-Created</option>
			<option value="discovered">Discovered</option>
			<option value="imported">Imported</option>
			<option value="manual">Manual</option>
			<option value="agent">Agent</option>
		</select>
		<select class="input text-xs" bind:value={filterImportState} on:change={firstPage}>
			<option value="">All import states</option>
			<option value="pending">Pending</option>
			<option value="adopted">Adopted</option>
			<option value="ignored">Ignored</option>
		</select>
		<select class="input text-xs" bind:value={filterEnv} on:change={firstPage} aria-label="Filter by env">
			<option value="">All envs</option>
			{#each ENVS.filter((e) => e !== 'template') as env}<option value={env}>{env}</option>{/each}
		</select>
		<label class="flex items-center gap-1 text-xs text-muted self-center">
			<input type="checkbox" bind:checked={showTemplates} on:change={firstPage} />
			Show templates{#if !showTemplates && templatesHidden > 0}&nbsp;<span class="num-inline">({templatesHidden} hidden)</span>{/if}
		</label>
		{#if hasActiveFilters}
			<button class="btn btn-ghost text-xs self-center" on:click={clearFilters}>Clear filters</button>
		{/if}
		<span class="text-muted text-xs self-center">
			{#if total > items.length}
				{page * PAGE_SIZE + 1}-{page * PAGE_SIZE + items.length} of {total} hosts
			{:else}
				{total} host{total === 1 ? '' : 's'}
			{/if}
		</span>
	</div>

	{#if joining && joining.waiting + joining.refused > 0}
		<p class="text-xs flex flex-wrap items-center gap-x-s-2" data-testid="joining-strip">
			<Lamp state={joining.waiting > 0 ? 'warn' : 'off'} />
			<span class="text-ink">{joiningSentence(joining)}</span>
			<a class="text-accent hover:underline" href="{base}/hosts/agents">Open Agents</a>
		</p>
	{:else if joiningUnread}
		<p class="text-xs flex flex-wrap items-center gap-x-s-2" data-testid="joining-strip">
			<Lamp state="off" />
			<span class="text-muted">Could not read who is waiting to join the agent hub.</span>
			<a class="text-accent hover:underline" href="{base}/hosts/agents">Open Agents</a>
		</p>
	{/if}

	{#if items.some((h) => h.agent_id && !h.agent_connected)}
		<div class="flex justify-end">
			<button class="btn btn-ghost text-xs" on:click={selectDisconnectedAgents}
				title="Select every host whose agent has no live channel">Select disconnected agents</button>
		</div>
	{/if}

	{#if selectedIds.size > 0}
		<div class="flex gap-2 items-center">
			<span class="text-xs text-ink">{selectedIds.size} selected</span>
			{#if canWrite}
				<button class="btn btn-sm text-xs" on:click={() => bulk('adopt')}>Adopt</button>
				<button class="btn btn-sm text-xs" on:click={() => bulk('ignore')}>Ignore</button>
				<button class="btn btn-sm text-xs" on:click={() => bulk('enrich')}>Enrich</button>
				{#if agentForgettable.length}
					<button class="btn btn-sm btn-danger text-xs"
						on:click={() => (agentBatch = { kind: 'forget', targets: agentForgettable })}
					>Forget {agentForgettable.length} agent{agentForgettable.length === 1 ? '' : 's'}</button>
				{/if}
				{#if agentRevokable.length}
					<button class="btn btn-sm text-xs text-danger"
						on:click={() => (agentBatch = { kind: 'revoke', targets: agentRevokable })}
					>Revoke {agentRevokable.length} agent{agentRevokable.length === 1 ? '' : 's'}</button>
				{/if}
			{/if}
			<button class="btn btn-sm btn-ghost text-xs" on:click={() => (selectedIds = new Set())}>Clear</button>
		</div>
	{/if}

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load hosts.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}><Icon name="refresh" />Retry</button>
		</div>
	{:else if items.length === 0 && hasActiveFilters}
		<div class="card p-6 text-center space-y-3">
			<p class="prose-note text-sm">No hosts match the current filters.</p>
			<button class="btn btn-ghost text-xs" on:click={clearFilters}>Clear filters</button>
		</div>
	{:else if items.length === 0}
		<div class="card p-6 text-center space-y-1">
			<p class="prose-note text-sm">No hosts yet.</p>
			<p class="prose-note text-xs">
				Sync from Proxmox to discover guests, add a host by hand, or enroll an
				agent on <a class="text-accent hover:underline" href="{base}/hosts/agents">Agents</a> —
				any of the three puts a machine on this page.
			</p>
		</div>
	{:else}
		{#if narrow}
			<!-- Phones (plan 3.2 mobile): one card per machine - name and chips on
			     line one, addresses and env on line two, the same row menu as a
			     trailing button. Same groups, same menu, same power line. -->
			<div class="host-cards section-stack" data-testid="host-cards">
				{#each groups as g (g.id)}
					<section aria-label="{g.label} ({g.hosts.length})" data-group={g.id}>
						<h2 class="host-cards-head">
							{#if isCollapsible(g.id)}
								<button
									type="button"
									class="btn btn-ghost btn-xs"
									aria-expanded={!collapsed[g.id]}
									on:click={() => toggleGroup(g.id)}
								><Icon name={collapsed[g.id] ? 'chevron-right' : 'chevron-down'} class="mr-1" />{g.label} ({g.hosts.length})</button>
							{:else}
								<span class="section-title">{g.label} ({g.hosts.length})</span>
							{/if}
						</h2>
						{#if !collapsed[g.id]}
							<ul class="host-card-list">
								{#each g.hosts as h (h.id)}
									{@const chip = stateChip(h)}
									<li class="host-card" data-host={h.id}>
										<div class="host-card-line">
											<a href="{base}/hosts/{h.id}" class="text-accent font-mono host-card-name">{h.hostname}</a>
											{#if h.is_self}<span class="badge bg-accent-tint text-accent-strong">HomePilot</span>{/if}
											{#if h.is_template}
												<span class="badge bg-raised text-muted">template</span>
											{:else}
												<span class="badge {chip.cls}">{chip.text}</span>
											{/if}
											{#if powerLines[h.id]?.phase === 'working'}<span class="badge badge-neutral">pending</span>{/if}
											{#if h.absent_since}<span class="badge badge-failed">gone</span>{/if}
											<span class="host-card-menu">
												<HostRowMenu
													host={h}
													{canWrite}
													{canAdmin}
													busy={powerLines[h.id]?.phase === 'working'}
													forgetting={forgetting === h.id}
													on:power={(e) => onPowerAsk(h, e.detail)}
													on:adopt={() => adoptOne(h)}
													on:ignore={() => ignoreOne(h)}
													on:forget={() => forgetOne(h)}
												/>
											</span>
										</div>
										<div class="host-card-line host-card-sub">
											<span class="font-mono">
												{#if h.addresses?.length}
													{#each addressCell(h.addresses) as a, i (a.text)}
														{#if i > 0}<span aria-hidden="true">, </span>{/if}
														<span class={a.conflict ? 'text-danger' : ''}
															>{a.text}{#if a.conflict}<span class="sr-only"> - address clash</span>{/if}</span
														>
													{/each}
												{:else}
													{h.ip_address || 'no address'}
												{/if}
											</span>
											{#if h.proxmox_id != null}<span class="font-mono">VMID {h.proxmox_id}</span>{/if}
											{#if h.env}<span class="badge {envClass(h.env)}">{h.env}</span>{/if}
											{#if attentionReason(h)}<span class="text-warn">{attentionReason(h)}</span>{/if}
										</div>
										{#if powerLines[h.id]}
											{@const pl = powerLines[h.id]}
											<p
												class="power-line power-{pl.tone}"
												role={pl.phase === 'refused' ? 'alert' : 'status'}
												data-power-line={pl.phase}
											>
												<Lamp state={pl.tone === 'muted' ? 'half' : pl.tone} />
												<span>{pl.text}</span>
												{#if pl.phase !== 'working'}
													<button type="button" class="btn btn-ghost btn-xs" on:click={() => setLine(h.id, null)}>Dismiss</button>
												{/if}
											</p>
										{/if}
									</li>
								{/each}
							</ul>
						{/if}
					</section>
				{/each}
			</div>
		{:else}
		<div class="card hosts-table-scroll">
			<!-- ONE table, grouped by tbody (#549 F3): the groups share a column
			     system, so a host in "Needs attention" and one in "Managed" still
			     line up down the page - which is the whole reason rows earn a
			     table rather than a stack of cards. The header sticks to the top
			     of this box on a long fleet (plan 4.6). -->
			<table class="data-table hosts-table text-xs">
				<thead>
					<tr>
						<th class="text-left pr-2">
							<input
								type="checkbox"
								aria-label="Select all hosts"
								on:change={toggleSelectAll}
								checked={selectedIds.size > 0 && selectedIds.size === items.length} />
						</th>
						<th class="text-left col-primary">Host</th>
						<th class="num col-secondary">VMID</th>
						<th class="text-left col-secondary">Env</th>
						<th class="text-left col-secondary">Addresses</th>
						<th class="text-left col-secondary">Role</th>
						<th class="text-left col-secondary">Source</th>
						<th class="text-left col-secondary">Node</th>
						<th class="text-left"><span class="sr-only">Actions</span></th>
					</tr>
				</thead>
				{#each groups as g (g.id)}
					<tbody data-group={g.id}>
						<tr class="group-row">
							<th colspan="9" scope="colgroup" class="text-left py-s-1">
								{#if isCollapsible(g.id)}
									<button
										type="button"
										class="btn btn-ghost btn-xs"
										aria-expanded={!collapsed[g.id]}
										on:click={() => toggleGroup(g.id)}
									><Icon name={collapsed[g.id] ? 'chevron-right' : 'chevron-down'} class="mr-1" />{g.label} ({g.hosts.length})</button>
								{:else}
									<span class="section-title">{g.label} ({g.hosts.length})</span>
								{/if}
							</th>
						</tr>
						{#if !collapsed[g.id]}
							{#each g.hosts as h (h.id)}
								{@const chip = stateChip(h)}
								<tr class="border-b border-divider hover:bg-raised transition-colors">
									<td class="pr-2">
										<input
											type="checkbox"
											aria-label="Select {h.hostname}"
											checked={selectedIds.has(h.id)}
											on:change={() => toggleSelect(h.id)} />
									</td>
									<!-- The primary column: the machine, and what state it is in.
									     Everything else on the row is supporting evidence and is
									     painted as such (#549 F1, principle 4). -->
									<td class="col-primary" title={attentionReason(h) || undefined}>
										<span class="col-primary-inner">
											<a
												href="{base}/hosts/{h.id}"
												class="text-accent hover:text-accent-strong font-mono"
											>{h.hostname}</a>
											{#if h.is_self}
												<!-- The machine HomePilot runs on, as an operator confirmed. -->
												<span class="badge bg-accent-tint text-accent-strong"
													title="HomePilot runs on this host">HomePilot</span>
											{/if}
											{#if h.is_template}
												<!-- A template never runs: "offline" would be noise. -->
												<span class="badge bg-raised text-muted" title="Proxmox template: a provisioning input">template</span>
											{:else}
												<span class="badge {chip.cls}">{chip.text}</span>
											{/if}
											{#if h.agent_id}
												<span
													class="badge {h.agent_connected
														? 'bg-ok-tint text-ok'
														: 'bg-raised text-muted'}"
													title={h.agent_connected
														? `Agent ${h.agent_version ?? ''} connected`
														: 'Agent enrolled but not connected'}
												>{h.agent_connected ? 'agent ·' : 'agent ∅'}{h.agent_connected && h.agent_version ? ` ${h.agent_version}` : ''}</span>
											{/if}
											{#if powerLines[h.id]?.phase === 'working'}
												<!-- The old chip stays until the server says otherwise (plan 5.4). -->
												<span class="badge badge-neutral" title="A power action is running">pending</span>
											{/if}
											{#if h.absent_since}
												<!-- A destroyed guest used to look exactly like a powered-off
												     one: the row simply stopped being updated (#445 A5). -->
												<span
													class="badge badge-failed"
													title="Proxmox stopped reporting this host on {fmtDate(h.absent_since)}"
												>gone</span>
											{/if}
											{#if h.import_state === 'ignored'}
												<span class="badge badge-superseded" title="Ignored: HomePilot leaves this host alone">ignored</span>
											{/if}
										</span>
									</td>
									<td class="num col-secondary font-mono">{h.proxmox_id ?? '—'}</td>
									<td class="col-secondary">
										{#if h.env}
											<span class="badge {envClass(h.env)}" title={envSourceLabel(h.env_source)}>{h.env}</span>
										{:else}
											—
										{/if}
									</td>
									<td class="col-secondary font-mono">
										<!-- Every address it holds (lane B2); a clashing one is
										     marked and says so to a screen reader too. -->
										{#if h.addresses?.length}
											{#each addressCell(h.addresses) as a, i (a.text)}
												{#if i > 0}<span aria-hidden="true">, </span>{/if}
												<span
													class={a.conflict ? 'text-danger' : ''}
													title={a.conflict ? 'Another machine holds this address too' : undefined}
													>{a.text}{#if a.conflict}<span class="sr-only"> - address clash</span>{/if}</span
												>
											{/each}
										{:else}
											{h.ip_address || '—'}
										{/if}
									</td>
									<td class="col-secondary">
										{h.role ?? '—'}
										{#if h.role_source === 'inferred'}<span class="text-warn" title="Inferred">?</span>{/if}
									</td>
									<td class="col-secondary">
										{#if h.source}
											<span class="px-1.5 py-0.5 rounded text-[10px] font-medium {sourceClass(h.source)}">{sourceBadge(h.source)}</span>
										{:else}
											—
										{/if}
									</td>
									<td class="col-secondary">{h.node ?? '—'}</td>
									<!-- ONE actions affordance per row (#549 F1 / #514 P7), now with
									     Console and the state-aware power verbs (UI slice 5). -->
									<td>
										<HostRowMenu
											host={h}
											{canWrite}
											{canAdmin}
											busy={powerLines[h.id]?.phase === 'working'}
											forgetting={forgetting === h.id}
											on:power={(e) => onPowerAsk(h, e.detail)}
											on:adopt={() => adoptOne(h)}
											on:ignore={() => ignoreOne(h)}
											on:forget={() => forgetOne(h)}
										/>
									</td>
								</tr>
								{#if powerLines[h.id]}
									{@const pl = powerLines[h.id]}
									<!-- The action's own line, under its row: working, settled, or
									     refused with the reason and the holder named. -->
									<tr class="power-row" data-power-for={h.id}>
										<td></td>
										<td colspan="8">
											<p
												class="power-line power-{pl.tone}"
												role={pl.phase === 'refused' ? 'alert' : 'status'}
												data-power-line={pl.phase}
											>
												<Lamp state={pl.tone === 'muted' ? 'half' : pl.tone} />
												<span>{pl.text}</span>
												{#if pl.phase !== 'working'}
													<button type="button" class="btn btn-ghost btn-xs" on:click={() => setLine(h.id, null)}>Dismiss</button>
												{/if}
											</p>
										</td>
									</tr>
								{/if}
							{/each}
						{/if}
					</tbody>
				{/each}
			</table>
		</div>
		{/if}
		{#if total > PAGE_SIZE}
			<div class="flex gap-2 items-center justify-end">
				<button
					class="btn btn-ghost text-xs"
					disabled={page === 0}
					on:click={() => {
						page -= 1;
						load();
					}}><Icon name="arrow-left" />Previous</button
				>
				<span class="text-xs text-muted">
					Page {page + 1} of {Math.ceil(total / PAGE_SIZE)}
				</span>
				<button
					class="btn btn-ghost text-xs"
					disabled={(page + 1) * PAGE_SIZE >= total}
					on:click={() => {
						page += 1;
						load();
					}}>Next<Icon name="arrow-right" /></button
				>
			</div>
		{/if}
	{/if}
</div>


{#if agentBatch}
	<div class="fixed inset-0 z-40 flex items-center justify-center bg-black/40" role="dialog" aria-modal="true">
		<div class="card p-5 max-w-md w-full space-y-3">
			<h3 class="font-semibold text-ink">
				{agentBatch.kind === 'forget' ? 'Forget' : 'Revoke'}
				{agentBatch.targets.length} agent{agentBatch.targets.length === 1 ? '' : 's'}?
			</h3>
			<p class="text-muted text-sm">
				{agentBatch.kind === 'forget'
					? 'Each agent and its credential are removed for good. The hosts stay in inventory; re-enrolling needs a fresh token.'
					: 'Each credential dies and any open channel closes now. Re-enrolling needs a fresh token.'}
			</p>
			<ul class="text-sm text-ink max-h-40 overflow-y-auto space-y-0.5">
				{#each agentBatch.targets as t (t.id)}
					<li class="font-mono">{t.hostname}</li>
				{/each}
			</ul>
			<div class="flex justify-end gap-2">
				<button class="btn btn-ghost text-xs" disabled={agentWorking} on:click={() => (agentBatch = null)}>Cancel</button>
				<button class="btn btn-danger text-xs" disabled={agentWorking} on:click={runAgentBatch}>
					{agentWorking ? 'Working…' : `${agentBatch.kind === 'forget' ? 'Forget' : 'Revoke'} ${agentBatch.targets.length}`}
				</button>
			</div>
		</div>
	</div>
{/if}

{#if typedAsk}
	{@const t = typedAsk}
	<ConfirmDialog
		title={t.action === 'stop' ? `Stop ${t.host.hostname} (forced)` : `${VERB_LABEL[t.action]} ${t.host.hostname}`}
		confirmName={t.host.hostname}
		confirmLabel="{VERB_LABEL[t.action]} {t.host.hostname}"
		danger={t.action !== 'start'}
		on:cancel={() => (typedAsk = null)}
		on:confirm={(e) => confirmTyped(e.detail.typed)}
	>
		<p data-confirm-consequence>
			{#if t.action === 'stop'}
				This cuts power to VMID {t.state.vmid} on {t.state.node} without a shutdown. Open files
				may be lost.
			{:else if t.action === 'shutdown'}
				This asks VMID {t.state.vmid} on {t.state.node} to shut down. If it has not within two
				minutes it is left running and you are told; nothing is forced.
			{:else if t.action === 'reboot'}
				A Proxmox reboot of VMID {t.state.vmid} on {t.state.node}: it shuts down and starts
				again{#if t.state.pending.length}, applying {t.state.pending.length} pending change{t.state
						.pending.length === 1
						? ''
						: 's'}{/if}.
			{:else}
				This starts VMID {t.state.vmid} on {t.state.node}. It is refused, with the holder named,
				if a running guest holds one of its addresses or its passthrough disk.
			{/if}
		</p>
		{#if t.state.is_self}
			<p class="text-danger" data-confirm-self>
				This is the machine HomePilot runs on. HomePilot goes down with it and this page will stop
				responding.
			</p>
		{:else if t.state.protected}
			<p data-confirm-protected>
				{t.host.hostname} is protected{t.host.env === 'infra' && t.host.protected == null
					? ' (env infra)'
					: ''}: switching it off takes its name typed back.
			</p>
		{/if}
	</ConfirmDialog>
{/if}

<style>
	/* The fleet table scrolls in its own box so its header can stick on a long
	   fleet (plan 4.6); a short fleet never fills it and never scrolls. */
	.hosts-table-scroll {
		overflow: auto;
		max-height: calc(100dvh - 7rem);
		padding-top: 0;
	}
	.hosts-table thead th {
		position: sticky;
		top: 0;
		z-index: 2;
		background: var(--color-surface);
	}
	.hosts-table tr.group-row th {
		background: var(--color-surface-raised);
	}
	.hosts-table tr.power-row td {
		padding-top: 0;
	}
	/* The line wraps inside the row's width and never widens a column: a
	   long refusal must not shift every row of the table sideways. */
	.hosts-table tr.power-row .power-line {
		width: 0;
		min-width: 100%;
	}

	/* A power action's line: a sentence, so it is set in the reading serif;
	   the lamp and the word carry the tone together, never colour alone. */
	.power-line {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2);
		margin: 0;
		font-family: var(--font-serif);
		font-size: 0.8125rem;
		line-height: 1.45;
		color: var(--color-text);
	}
	.power-muted {
		color: var(--color-muted);
	}
	.power-warn {
		color: var(--color-warn);
	}
	.power-crit {
		color: var(--color-danger);
	}

	/* The disabled "Provision" door in `+ Add`: it says why, legibly. */
	.add-later {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		opacity: 1;
		border-style: dashed;
	}
	.add-later:disabled {
		opacity: 1;
	}
	.menu-why {
		font-size: 0.6875rem;
		line-height: 1.3;
		font-weight: 400;
		color: var(--color-muted);
		white-space: normal;
	}

	/* Phones: one card per machine (plan 3.2 mobile). */
	.host-cards-head {
		margin: 0 0 var(--space-1);
	}
	.host-card-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}
	.host-card {
		background: var(--color-surface);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-sm);
		padding: var(--space-2) var(--space-3);
	}
	.host-card-line {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-1) var(--space-2);
		min-width: 0;
	}
	.host-card-name {
		font-size: 0.875rem;
		overflow-wrap: anywhere;
	}
	.host-card-menu {
		margin-left: auto;
	}
	.host-card-sub {
		margin-top: var(--space-1);
		font-size: 0.75rem;
		color: var(--color-muted);
	}
	.host-card .power-line {
		margin-top: var(--space-2);
	}
</style>
