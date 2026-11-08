// How an address is shown (lane B2): where it came from, and how old that is.
//
// One address can be stated by several sources at once - the cloud-init line
// that configures it and the guest agent that sees it held - and an operator
// reading "10.0.0.5" needs to know which. Pure, so the wording is assertable
// without a DOM.

import type { AddressSource, HostAddress, HostAddressBrief } from './api';

const SOURCE_LABELS: Record<AddressSource, string> = {
	'qemu-ipconfig': 'cloud-init',
	'lxc-net': 'container config',
	'pve-node': 'node interface',
	manual: 'typed by an operator',
	'guest-agent': 'guest agent',
	arp: 'answered ARP',
};

/** Sources that say what the machine WILL use; the rest say what was SEEN. */
export const CONFIGURED_SOURCES: readonly AddressSource[] = [
	'qemu-ipconfig',
	'lxc-net',
	'pve-node',
	'manual',
];

export function sourceLabel(source: AddressSource | string): string {
	return SOURCE_LABELS[source as AddressSource] ?? source;
}

export function isObserved(source: AddressSource | string): boolean {
	return !CONFIGURED_SOURCES.includes(source as AddressSource);
}

/**
 * Where an address is configured: `net0 on vmbr0 (VLAN 20)`. A node's own
 * interface is its bridge, so it reads just `vmbr0`.
 */
export function wireText(a: Pick<HostAddress, 'iface' | 'bridge' | 'vlan'>): string {
	const parts: string[] = [];
	if (a.iface && a.iface !== a.bridge) parts.push(a.iface);
	if (a.bridge) parts.push(parts.length ? `on ${a.bridge}` : a.bridge);
	if (a.vlan) parts.push(`(VLAN ${a.vlan})`);
	return parts.join(' ');
}

/** The host list's address cell: addresses in order, the clashing ones marked. */
export function addressCell(addresses: HostAddressBrief[] | undefined): {
	text: string;
	conflict: boolean;
}[] {
	return (addresses ?? []).map((a) => ({
		text: a.conflict ? `${a.address} (!)` : a.address,
		conflict: a.conflict,
	}));
}
