// Keep a bulk-action selection honest against what the table actually shows.
//
// Inventory selection survived a filter change, so "3 selected → Adopt" could
// adopt hosts that were no longer on screen (and the count lied about what was
// about to happen). Every reload prunes the selection down to the visible rows.

export function pruneSelection(
	selected: Iterable<string>,
	visible: readonly { id: string }[],
): Set<string> {
	const visibleIds = new Set(visible.map((row) => row.id));
	const next = new Set<string>();
	for (const id of selected) {
		if (visibleIds.has(id)) next.add(id);
	}
	return next;
}
