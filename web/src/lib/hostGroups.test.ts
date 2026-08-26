import { describe, it, expect, beforeEach } from 'vitest';
import {
	attentionReason,
	COLLAPSE_ABOVE,
	groupHosts,
	groupOf,
	initialCollapsed,
	isCollapsible,
	isUnadopted,
	readCollapsePreference,
	writeCollapsePreference,
} from './hostGroups';
import type { Host } from './api';

/**
 * The fleet's three populations (#549 F3, principle 1).
 *
 * Teeth: reorder the groups and the priority test fails; let an un-adopted host
 * raise attention and the "ignored machines do not shout" test fails; drop the
 * empty-group filter and the last test fails on a heading that describes nothing.
 */
function h(over: Partial<Host> & { id: string }): Host {
	return {
		hostname: over.id,
		status: 'online',
		source: 'discovered',
		import_state: 'adopted',
		...over,
	} as Host;
}

describe('attentionReason', () => {
	it('names offline, gone and a dead agent channel', () => {
		expect(attentionReason(h({ id: 'a', status: 'offline' }))).toBe('offline');
		expect(attentionReason(h({ id: 'b', absent_since: '2026-08-01T00:00:00Z' }))).toBe('gone');
		expect(attentionReason(h({ id: 'c', agent_id: 'ag', agent_connected: false }))).toBe(
			'agent offline',
		);
	});

	it('says nothing about a healthy managed host', () => {
		expect(attentionReason(h({ id: 'd', agent_id: 'ag', agent_connected: true }))).toBe('');
	});

	it('lets an un-adopted or ignored machine stay quiet', () => {
		// A discovered guest nobody asked HomePilot to manage is not a fault, and
		// an ignored one is a decision already taken. Either shouting is how an
		// attention zone becomes wallpaper.
		expect(attentionReason(h({ id: 'e', status: 'offline', import_state: 'pending' }))).toBe('');
		expect(attentionReason(h({ id: 'f', status: 'offline', import_state: 'ignored' }))).toBe('');
	});
});

describe('isUnadopted', () => {
	it('is about the adoption decision, not the source', () => {
		expect(isUnadopted(h({ id: 'a', source: 'manual', import_state: 'adopted' }))).toBe(false);
		expect(isUnadopted(h({ id: 'b', source: 'discovered', import_state: 'pending' }))).toBe(true);
		expect(isUnadopted(h({ id: 'c', source: 'discovered', import_state: 'ignored' }))).toBe(true);
	});
});

describe('groupHosts', () => {
	const FLEET: Host[] = [
		h({ id: 'healthy' }),
		h({ id: 'pending', import_state: 'pending' }),
		h({ id: 'broken', status: 'offline' }),
	];

	it('puts what needs an operator FIRST, enumeration last', () => {
		expect(groupHosts(FLEET).map((g) => g.id)).toEqual(['attention', 'managed', 'discovered']);
	});

	it('counts each group', () => {
		expect(groupHosts(FLEET).map((g) => [g.id, g.hosts.length])).toEqual([
			['attention', 1],
			['managed', 1],
			['discovered', 1],
		]);
	});

	it('never loses a host', () => {
		const all = groupHosts(FLEET).flatMap((g) => g.hosts.map((x) => x.id));
		expect(all.sort()).toEqual(['broken', 'healthy', 'pending']);
	});

	it('drops empty groups rather than rendering a false alarm', () => {
		// An always-present "Needs attention" heading over nothing trains an
		// operator to ignore the heading.
		expect(groupHosts([h({ id: 'only-healthy' })]).map((g) => g.id)).toEqual(['managed']);
		expect(groupHosts([]).length).toBe(0);
	});

	it('agrees with groupOf', () => {
		for (const g of groupHosts(FLEET)) {
			for (const host of g.hosts) expect(groupOf(host)).toBe(g.id);
		}
	});
});

describe('collapse defaults', () => {
	beforeEach(() => {
		try {
			globalThis.localStorage?.clear();
		} catch {
			/* nothing stored, nothing to clear */
		}
	});

	it('only the healthy group collapses - a fault may never hide', () => {
		expect(isCollapsible('managed')).toBe(true);
		expect(isCollapsible('attention')).toBe(false);
		expect(isCollapsible('discovered')).toBe(false);
	});

	it('collapses the healthy group once the fleet outgrows a screenful', () => {
		expect(initialCollapsed('managed', COLLAPSE_ABOVE)).toBe(false);
		expect(initialCollapsed('managed', COLLAPSE_ABOVE + 1)).toBe(true);
		// ...and never collapses a group that may hold a fault.
		expect(initialCollapsed('attention', 500)).toBe(false);
	});

	it("remembers the operator's own choice over the size default", () => {
		writeCollapsePreference('managed', false);
		expect(readCollapsePreference('managed')).toBe(false);
		// A big fleet no longer re-collapses what they deliberately opened.
		expect(initialCollapsed('managed', 500)).toBe(false);
	});

	it('survives storage being unavailable', () => {
		const store = globalThis.localStorage;
		Object.defineProperty(globalThis, 'localStorage', {
			configurable: true,
			get() {
				throw new Error('blocked by policy');
			},
		});
		try {
			expect(readCollapsePreference('managed')).toBeNull();
			expect(() => writeCollapsePreference('managed', true)).not.toThrow();
			expect(initialCollapsed('managed', 3)).toBe(false);
		} finally {
			Object.defineProperty(globalThis, 'localStorage', {
				configurable: true,
				value: store,
				writable: true,
			});
		}
	});
});
