// The fleet model's vocabulary (lane B1, review 02 steps 1-3).

import type { Host } from './api';

/**
 * The environments a host can belong to. MUST equal ENVS in
 * src/homepilot/inventory/fleet_model.py and the CHECK on hosts.env - the UI
 * may not offer a value the server refuses.
 */
export const ENVS = ['prod', 'dev', 'infra', 'friend', 'lab', 'template'] as const;

/** Where a host's env came from, in words an operator reads. */
export function envSourceLabel(source: Host['env_source']): string {
	if (source === 'user') return 'set here';
	if (source === 'pve_tag') return 'from the Proxmox tag';
	if (source === 'pve_template') return 'a Proxmox template';
	return '';
}

/** The chip colour for an env: production is the one that must stand out. */
export function envClass(env?: string | null): string {
	if (env === 'prod') return 'bg-danger-tint text-danger';
	if (env === 'infra') return 'bg-accent-tint text-accent-strong';
	return 'bg-raised text-muted';
}

/** PVE's tag string as a list (it separates with `;`). */
export function pveTags(raw?: string | null): string[] {
	return (raw ?? '')
		.split(/[;,\s]+/)
		.map((t) => t.trim())
		.filter(Boolean);
}

/** Proxmox's allocation as one short line: `2c · 2.0G RAM · 5G disk`. */
export function allocation(h: Host): string {
	const parts: string[] = [];
	if (h.maxcpu) parts.push(`${h.maxcpu}c`);
	if (h.maxmem_mb) parts.push(`${(h.maxmem_mb / 1024).toFixed(1)}G RAM`);
	if (h.maxdisk_gb) parts.push(`${h.maxdisk_gb}G disk`);
	return parts.join(' · ');
}
