<script lang="ts">
	import '../app.css';
	import { onMount, onDestroy } from 'svelte';
	import { goto, afterNavigate } from '$app/navigation';
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import { toast } from '$lib/stores';
	import { hasCookieSession, getToken, api, sessionStore, refreshSession, setToken } from '$lib/api';
	
	import { startEventStream, stopEventStream, streamConnected, onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { scopeTier } from '$lib/scopeTier';
	import { isAdmin as capIsAdmin } from '$lib/capabilities';
	import {
		NAV_GROUPS,
		visibleNav as filterNav,
		navCounts,
		guestsGateOpen,
		isCurrentNav,
		shellCounts,
		type NavCount,
		type NavCountKey,
	} from '$lib/nav';
	import { MAKER_MARK } from '$lib/brand';
	import Icon from '$lib/components/Icon.svelte';
	import Lamp from '$lib/components/Lamp.svelte';
	import ThemeControl from '$lib/components/ThemeControl.svelte';
	import CommandPalette from '$lib/components/CommandPalette.svelte';

	// Seven nouns in four jobs (UI slice 1, owner decisions 2026-11-08): the
	// glance / the estate / what changes it / the record and plumbing. The
	// model and its gating live in $lib/nav so they are testable without the
	// shell; the admin-only surfaces inside the pages gate their own controls.
	let counts: Partial<Record<NavCountKey, NavCount>> = {};
	let guestsOpen = false;

	$: current = $page.url.pathname;
	// Guests stays visible while the operator is ON it (they arrived through
	// the Settings door before the first invite), so the shell never hides
	// the page you are reading.
	$: groups = filterNav(NAV_GROUPS, {
		guests: guestsOpen || isCurrentNav('/guests', current, base),
	});

	$: isLoginRoute = $page.url.pathname === base + '/login';

	// Derive the sidebar session straight from the reactive store so it tracks
	// login/logout without a full remount (the layout persists across the
	// login → app navigation, so a mount-only read went permanently stale).
	$: me = $sessionStore;
	let sessionLoading = true;
	// Mobile nav state. Closed by default and closed again on navigation - a
	// drawer left open over the page a tap just loaded is its own dead end.
	let navOpen = false;
	// The command palette (UI slice 2): Ctrl+K or "/" from any page, or the
	// Search control in the sidebar / the phone bar.
	let paletteOpen = false;

	function toLogin() {
		const returnTo = encodeURIComponent($page.url.pathname + $page.url.search);
		goto(`${base}/login?returnTo=${returnTo}`, { replaceState: true });
	}

	async function syncSession() {
		// Two credentials can carry a session: the HttpOnly cookie (normal path)
		// and the in-memory bearer token (the documented fallback when the cookie
		// cannot be set — e.g. a cross-origin API base). Bouncing on "no cookie"
		// alone dead-ended that fallback at the first navigation click, so check
		// BOTH; with neither, redirect without spending a request.
		const hasCredential = hasCookieSession() || getToken() !== '';
		if (!hasCredential && !isLoginRoute) {
			toLogin();
			return;
		}
		const me = await refreshSession();
		// A credential that the server rejects is no session at all.
		if (!me && !isLoginRoute) {
			sessionLoading = false;
			toLogin();
			return;
		}
		sessionLoading = false;
		// Open the shared SSE stream once a session exists so live artifact/drift
		// updates flow to every page. Idempotent — safe to call on each re-sync.
		if (me) {
			startEventStream();
			void loadShellFacts(capIsAdmin(me.capabilities));
		}
	}

	/**
	 * The sidebar's numbers and the Guests gate, re-read on every navigation
	 * (the same moment the session is re-checked). Both are best-effort: a
	 * failed read leaves the count ABSENT, never zero, and the gate closed.
	 */
	async function loadShellFacts(admin: boolean) {
		const [summary, guests] = await Promise.allSettled([
			api.getDashboard(),
			admin ? api.getGuests() : Promise.resolve(null),
		]);
		counts = navCounts(summary.status === 'fulfilled' ? summary.value : null);
		shellCounts.set(counts);
		guestsOpen = guestsGateOpen(guests.status === 'fulfilled' ? guests.value : null, admin);
	}

	onMount(syncSession);
	onDestroy(stopEventStream);
	// A change approved, applied or withdrawn anywhere moves the Changes count at
	// once (UI_PLAN 5.5), not on the next navigation: the count IS the promise
	// that nothing pending is out of sight.
	const refreshCounts = debounce(() => {
		const me = $sessionStore;
		if (me) void loadShellFacts(capIsAdmin(me.capabilities));
	}, 500);
	const unsubCounts = onArtifactEvent(() => refreshCounts());
	onDestroy(() => {
		unsubCounts();
		refreshCounts.cancel();
	});
	// Re-check after client-side navigations (e.g. login → app) so the session
	// panel updates. Skip the initial 'enter' — onMount already covers it.
	afterNavigate((nav) => {
		// However the navigation happened - a link, the back button, a redirect -
		// the drawer must not be left open over the page it just loaded.
		navOpen = false;
		if (nav.type === 'enter') return;
		syncSession();
	});

	// Escape closes the drawer. It is the one overlay in the UI, and an overlay
	// a keyboard user cannot dismiss is a trap.
	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && navOpen) navOpen = false;
	}

	async function handleLogout() {
		if (typeof window !== 'undefined' && !window.confirm('Log out of this browser session?')) return;
		// Cookie-only sign-out: clears the session cookie server-side but does NOT
		// revoke the API token, so the same token still works for CLI/MCP and for
		// logging back in. Clear local state and return to the login screen.
		try { await api.logout(); } catch { /* ignore network errors */ }
		stopEventStream();
		setToken('');
		sessionStore.set(null);
		sessionLoading = false;
		goto(`${base}/login`);
	}

	// The access tier, from the normalized capabilities (#704).
	$: tier = me ? scopeTier(me.capabilities, me.scope) : null;
</script>

<svelte:window on:keydown={onKeydown} />

{#if isLoginRoute}
	<slot />
{:else}
<div class="flex flex-col md:flex-row min-h-screen">
	<!-- Mobile bar: the sidebar is a fixed 176px column, which on a phone left
	     almost nothing for the tables that ARE the product (#445 B6). Below md
	     the nav collapses behind this bar instead. -->
	<div class="md:hidden flex items-center gap-3 px-4 py-2 bg-surface border-b border-border">
		<button
			class="btn btn-ghost"
			aria-expanded={navOpen}
			aria-controls="main-nav"
			on:click={() => (navOpen = !navOpen)}
		>
			<span class="sr-only">{navOpen ? 'Close navigation' : 'Open navigation'}</span>
			<Icon name={navOpen ? 'close' : 'menu'} size={16} />
		</button>
		<span class="wordmark">Home<em>Pilot</em></span>
		<button
			class="btn btn-ghost ml-auto"
			aria-label="Search (Ctrl+K)"
			on:click={() => (paletteOpen = true)}
			data-palette-trigger
		>
			<Icon name="search" size={16} />
		</button>
	</div>

	<!-- Sidebar -->
	<nav
		id="main-nav"
		aria-label="Main"
		class="shell-side w-full md:w-44 shrink-0 bg-surface border-b md:border-b-0 md:border-r border-border
		       flex-col pt-s-4 pb-s-3 {navOpen ? 'flex' : 'hidden'} md:flex"
	>
		<div class="px-4 pb-s-4 hidden md:block">
			<span class="wordmark">Home<em>Pilot</em></span>
		</div>
		<!-- The palette's visible door: the chord is named on it, so the
		     shortcut is learned by using the button. -->
		<button
			class="side-search hidden md:flex"
			on:click={() => (paletteOpen = true)}
			data-palette-trigger
		>
			<Icon name="search" />
			<span class="flex-1 text-left">Search</span>
			<span class="kbd font-mono" aria-hidden="true">Ctrl K</span>
		</button>
		{#each groups as group (group.label ?? '')}
			{#if group.label}
				<!-- A group label is a sentence-case word in the chrome, not an
				     uppercase letterspaced eyebrow. -->
				<div class="nav-group" aria-hidden="true">{group.label}</div>
			{/if}
			{#each group.items as item (item.href)}
				{@const c = item.count ? counts[item.count] : undefined}
				{@const on = isCurrentNav(item.href, current, base)}
				<a
					href="{base}{item.href}"
					on:click={() => (navOpen = false)}
					aria-current={on ? 'page' : undefined}
					class="nav-link {on ? 'is-current' : ''}"
					data-nav={item.href}
				>
					<span>{item.label}</span>
					{#if c}
						<span class="nav-count nav-count-{c.tone}" data-count={item.count}>
							<span aria-hidden="true">{c.n}</span><span class="sr-only">, {c.label}</span>
						</span>
					{/if}
				</a>
			{/each}
		{/each}

		<div class="side-foot mt-auto">
			{#if me}
				<!-- Who is signed in and what they may do, said as a sentence (3.10.0
				     walk: a bare token label over a lone green "all" read as stray
				     words, not as the session). -->
				<div class="flex items-center gap-2 min-w-0" data-session-line>
					<Lamp state="ok" />
					<span class="text-muted truncate"
						>Signed in as <span class="text-ink">{me.token_label || me.prefix || 'this session'}</span
						></span
					>
				</div>
				<div class="flex items-center justify-between gap-2 min-w-0">
					{#if tier}
						<span class="text-muted truncate" data-session-access>{tier.label}</span>
					{/if}
					<button class="text-muted hover:text-danger transition-colors ml-auto" on:click={handleLogout}>
						Log out
					</button>
				</div>
			{:else if sessionLoading}
				<div class="flex items-center gap-2">
					<Lamp state="half" label="Loading…" class="text-muted" />
				</div>
			{:else}
				<div class="flex items-center gap-2">
					<Lamp state="crit" label="No session" class="text-muted" />
				</div>
			{/if}
			{#if me && !$streamConnected}
				<!-- The stream reconnects silently with backoff, so when it is down
				     the UI just stops updating. "Nothing is happening" and "I am not
				     being told what is happening" are opposite conclusions on an ops
				     console (#435). -->
				<div title="Reconnecting to the live update stream">
					<Lamp state="warn" label="Live updates offline" class="text-warn" />
				</div>
			{/if}
			<ThemeControl />
			<!-- Maker's mark: quiet and muted, the foot of the shell, never in
			     the nav list or a hero. -->
			<a class="maker-mark block" href={MAKER_MARK.href} target="_blank" rel="noopener">{MAKER_MARK.text}</a>
		</div>
	</nav>

	<!-- Main -->
	<!-- `min-w-0` is what actually lets the wide tables scroll inside their own
	     container: without it a flex child refuses to shrink below its content
	     and the whole PAGE scrolls sideways instead. -->
	<main class="flex-1 min-w-0 overflow-auto px-4 py-s-4 md:px-5 flex flex-col">
		<div class="flex-1"><slot /></div>
		<!-- Below md the sidebar is a drawer, so the mark would be hidden with
		     it; the page carries it instead. One mark is visible at any width. -->
		<footer class="md:hidden mt-s-6 pt-s-4 border-t border-divider flex justify-end">
			<a class="maker-mark" href={MAKER_MARK.href} target="_blank" rel="noopener">{MAKER_MARK.text}</a>
		</footer>
	</main>
</div>
{/if}

{#if !isLoginRoute && me}
	<CommandPalette bind:open={paletteOpen} guests={guestsOpen || isCurrentNav('/guests', current, base)} />
{/if}

<!-- Toast — the live region is always mounted (a region added at the same time
     as its text is not reliably announced) and errors are assertive. -->
<div
	class="fixed bottom-6 right-6 z-30"
	role={$toast?.kind === 'err' ? 'alert' : 'status'}
	aria-live={$toast?.kind === 'err' ? 'assertive' : 'polite'}
	aria-atomic="true"
>
	{#if $toast}
		<div
			class="px-4 py-3 rounded-md text-sm shadow-lg border
			       {$toast.kind === 'ok'
			         ? 'bg-ok-tint border-ok-border text-ok'
			         : 'bg-danger-tint border-danger-border text-danger'}"
		>
			{$toast.msg}
		</div>
	{/if}
</div>
<style>
	.nav-group {
		padding: 0.75rem 1rem 0.25rem;
		font-size: 0.6875rem;
		font-weight: 700;
		color: var(--color-muted);
	}
	.nav-link {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.375rem 1rem 0.375rem calc(1rem - 3px);
		border-left: 3px solid transparent;
		color: var(--color-text);
		font-size: 0.8125rem;
		transition: background-color 120ms ease, color 120ms ease;
	}
	.nav-link:hover {
		background: var(--color-surface-raised);
		color: var(--color-text-strong);
	}
	.nav-link.is-current {
		background: var(--color-surface-raised);
		border-left-color: var(--color-accent);
		color: var(--color-text-strong);
	}
	/* Counts are mono numerals in a small box; the tone repeats the meaning
	   the spoken label already carries. */
	.nav-count {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 600;
		line-height: 1;
		padding: 0.125rem 0.375rem;
		border-radius: var(--radius-sm);
		border: 1px solid var(--color-border);
		background: var(--color-surface-raised);
		color: var(--color-muted);
	}
	.nav-count-warn {
		color: var(--color-warn);
		border-color: var(--color-warn);
	}
	.nav-count-crit {
		color: var(--color-danger);
		border-color: var(--color-danger);
	}
	.side-search {
		align-items: center;
		gap: 0.5rem;
		margin: 0 0.75rem 0.5rem;
		padding: 0.3125rem 0.5rem;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-sm);
		background: var(--color-bg);
		color: var(--color-muted);
		font-size: 0.75rem;
		transition: border-color 120ms ease, color 120ms ease;
	}
	.side-search:hover {
		border-color: var(--color-border-strong);
		color: var(--color-text);
	}
	.side-foot {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		margin-top: auto;
		padding: 0.625rem 1rem 0;
		border-top: 1px solid var(--color-border);
		font-size: 0.6875rem;
		color: var(--color-muted);
	}
</style>
