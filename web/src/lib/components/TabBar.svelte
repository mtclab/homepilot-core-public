<script lang="ts">
	// The ONE tab bar (#549 F1, principle 2).
	//
	// Changes and Records each carried their own copy of the same flex row of
	// anchors; the Host page and Settings are about to want a third and a
	// fourth. This is that row, extracted, made keyboard-reachable and given
	// the WAI-ARIA tabs roles it never had.
	//
	// It is deliberately dumb: which tab is current is decided by `$lib/tabs`
	// from the URL and handed in, so the same widget serves sub-route tabs and
	// `?tab=` tabs without knowing the difference — and so the addressing can
	// be asserted without a DOM.
	//
	// Activation is MANUAL (WAI-ARIA: use it when activation has significant
	// consequences). Arrow/Home/End move focus along the row; Enter follows the
	// link. Auto-activation would fire a route load on every arrow press.
	import type { Tab } from '$lib/tabs';

	export let tabs: Tab[] = [];
	/** Id of the current tab, from `activeRouteTab` / `activeQueryTab`. */
	export let activeId = '';
	/** Names the tab row for screen readers, e.g. "Changes views". */
	export let label: string;
	/** Id of the element holding the tab's content, if the caller renders one. */
	export let panelId = '';

	let listEl: HTMLDivElement | undefined;

	/**
	 * Roving tabindex: exactly one tab is in the tab order. When the URL is
	 * outside the group (no tab matches) that is the first one, so the row is
	 * never keyboard-unreachable.
	 */
	$: rovingIndex = Math.max(
		0,
		tabs.findIndex((t) => t.id === activeId),
	);

	function focusAt(i: number): void {
		const els = listEl?.querySelectorAll<HTMLAnchorElement>('[role="tab"]');
		if (!els || els.length === 0) return;
		els[((i % els.length) + els.length) % els.length].focus();
	}

	function onKeydown(event: KeyboardEvent, i: number): void {
		switch (event.key) {
			case 'ArrowRight':
			case 'ArrowDown':
				focusAt(i + 1);
				break;
			case 'ArrowLeft':
			case 'ArrowUp':
				focusAt(i - 1);
				break;
			case 'Home':
				focusAt(0);
				break;
			case 'End':
				focusAt(tabs.length - 1);
				break;
			default:
				return;
		}
		event.preventDefault();
	}
</script>

<!-- `overflow-x-auto` + `shrink-0`: on a phone a seven-tab Settings row scrolls
     sideways instead of wrapping into a ragged block. -->
<div
	class="flex gap-1 border-b border-border pb-2 overflow-x-auto"
	role="tablist"
	aria-label={label}
	bind:this={listEl}
>
	{#each tabs as t, i (t.id)}
		<a
			id="tab-{t.id}"
			role="tab"
			href={t.href}
			aria-selected={t.id === activeId}
			aria-controls={panelId || undefined}
			tabindex={i === rovingIndex ? 0 : -1}
			on:keydown={(e) => onKeydown(e, i)}
			class="px-3 py-1 rounded text-sm shrink-0 whitespace-nowrap {t.id === activeId
				? 'bg-raised text-accent'
				: 'text-muted hover:text-ink'}">{t.label}</a>
	{/each}
</div>
