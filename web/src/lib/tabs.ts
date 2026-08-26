// Tab addressing — the pure half of the shared TabBar (#549 F1).
//
// Two ways a page can carry tabs, one vocabulary:
//
//   * SUB-ROUTES  — Changes and Records already do this (#514 S4): each tab is
//     its own route, so the tab IS the URL.
//   * QUERY STATE — a page that keeps one route (the Host page: its id is in
//     the path) addresses its sectors with `?tab=`.
//
// Both resolve to the same `Tab[]` shape (an identity plus a link), so the
// widget never has to know which kind it is rendering and both are equally
// bookmarkable. Keeping the addressing HERE — not inside the component — is
// what lets it be asserted without mounting anything.

/** A tab as the component renders it: an identity, a label and a target URL. */
export interface Tab {
	id: string;
	label: string;
	href: string;
}

/** A tab that is its own route. */
export interface RouteTabDef {
	id: string;
	label: string;
	/** App-relative path, WITHOUT the SvelteKit `base` (e.g. `/changes/drift`). */
	href: string;
	/**
	 * Match this tab only on its exact path. The index tab of a group needs it:
	 * `/changes` is a prefix of every other Changes route.
	 */
	exact?: boolean;
}

/** A tab that is `?tab=` state on one route. */
export interface QueryTabDef {
	id: string;
	label: string;
}

/** Route tabs, with `base` applied once. */
export function routeTabs(defs: RouteTabDef[], base: string): Tab[] {
	return defs.map((d) => ({ id: d.id, label: d.label, href: base + d.href }));
}

/**
 * Which route tab the current pathname belongs to.
 *
 * `detailPrefix` is the group's own prefix (e.g. `/ui/changes/`): a DETAIL route
 * under the group — `/changes/{id}` — belongs to the first tab, which is what
 * the Changes layout did by hand before this was shared. Returns `''` when the
 * pathname is outside the group entirely, so nothing is falsely marked current.
 */
export function activeRouteTab(
	defs: RouteTabDef[],
	pathname: string,
	base: string,
	detailPrefix = '',
): string {
	for (const d of defs) {
		const full = base + d.href;
		if (d.exact) {
			if (pathname === full || pathname === full + '/') return d.id;
		} else if (pathname === full || pathname.startsWith(full + '/')) {
			return d.id;
		}
	}
	if (detailPrefix && pathname.startsWith(detailPrefix) && defs.length > 0) return defs[0].id;
	return '';
}

/**
 * Query tabs as real links: `?tab=metrics` on the page's own path. They are
 * links and not buttons on purpose — a tab an operator cannot send to a
 * colleague is a tab that lost the argument with the URL bar. Any other query
 * the page is carrying (a search term, a page number) is preserved.
 */
export function queryTabs(
	defs: QueryTabDef[],
	pathname: string,
	param = 'tab',
	search?: URLSearchParams,
): Tab[] {
	return defs.map((d) => {
		const params = new URLSearchParams(search ?? undefined);
		params.set(param, d.id);
		return { id: d.id, label: d.label, href: `${pathname}?${params.toString()}` };
	});
}

/**
 * Which query tab a URL selects. An absent, empty or unrecognised value falls
 * back to the first tab rather than to nothing: a stale bookmark from before a
 * tab was renamed must still land on a real sector.
 */
export function activeQueryTab(defs: QueryTabDef[], url: URL, param = 'tab'): string {
	const raw = url.searchParams.get(param);
	if (raw && defs.some((d) => d.id === raw)) return raw;
	return defs.length > 0 ? defs[0].id : '';
}
