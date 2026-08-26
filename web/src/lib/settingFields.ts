// Which editable setting belongs to which subsystem card (#553 C2).
//
// The server's registry decides WHAT is editable; this decides WHERE it is
// edited, so a setting always sits on the card whose status it explains - the
// push interval next to "the last push succeeded", the embedding URL next to
// "KB search is keyword-only". Anything the server sends that this map does not
// place still renders, in its own group: a setting nobody can reach is worse
// than one in the wrong place.

import type { SettingOverride } from './api';

/** Subsystem name (from the self-check report) -> setting keys, in field order. */
export const SUBSYSTEM_SETTINGS: Record<string, string[]> = {
	artifacts_remote: ['artifacts_remote', 'artifacts_push_interval_seconds'],
	embeddings: ['embedding_service_url', 'embedding_model'],
	events_webhook: ['events_webhook_url'],
};

/**
 * Groups with no self-check subsystem of their own. Retention is real
 * configuration with no probe to report on - nothing to reach, so the report
 * says nothing about it - and it would otherwise be invisible here.
 */
export const EXTRA_GROUPS: Array<{ id: string; title: string; note: string; keys: string[] }> = [
	{
		id: 'retention',
		title: 'Retention',
		note: 'How long this instance keeps what it records. Pruning runs on a schedule; a shortened window takes effect on the next cycle.',
		keys: ['retention_days', 'metrics_retention_days'],
	},
	{
		id: 'provisioning',
		title: 'Provisioning defaults',
		note: 'What guest provisioning uses when a request does not say it itself, so an invite carries a person and a size rather than the cluster\u2019s topology. Each value is checked against the live cluster before it is saved; Test asks without saving. A bridge is per-node, so set the node first - and setting a bridge is what makes provisioning write net0 at all.',
		keys: [
			'provision_default_node',
			'provision_default_template_vmid',
			'provision_default_pool',
			'provision_default_bridge',
			'provision_default_vlan_tag',
			'provision_default_ipconfig',
		],
	},
];

/** Field labels. Falls back to the key, which is also the env var's stem. */
const FIELD_LABELS: Record<string, string> = {
	artifacts_remote: 'Remote',
	artifacts_push_interval_seconds: 'Push interval (seconds)',
	embedding_service_url: 'Service URL',
	embedding_model: 'Model',
	events_webhook_url: 'Webhook URL',
	retention_days: 'Operational history (days)',
	metrics_retention_days: 'Metric samples (days)',
	provision_default_node: 'Node',
	provision_default_template_vmid: 'Template VMID (0 = none)',
	provision_default_pool: 'Pool',
	provision_default_bridge: 'Bridge',
	provision_default_vlan_tag: 'VLAN tag (0 = untagged)',
	provision_default_ipconfig: 'ipconfig0',
	guest_network_zone: 'SDN zone',
	guest_network_vnet: 'Vnet (the bridge guests get)',
	guest_network_subnet: 'Subnet (CIDR)',
	guest_network_gateway: 'Gateway',
	guest_network_snat: 'SNAT (1 = on)',
	guest_network_dhcp: 'DHCP (1 = on)',
	guest_network_dhcp_range: 'DHCP range (start-end)',
	guest_network_dhcp_dns_server: 'DNS server handed to guests',
	guest_network_isolate_cidrs: 'Never reachable from a guest',
};

/** The eight settings that describe the guest network, in the order they are set. */
export const GUEST_NETWORK_KEYS: string[] = [
	'guest_network_subnet',
	'guest_network_gateway',
	'guest_network_zone',
	'guest_network_vnet',
	'guest_network_snat',
	'guest_network_dhcp',
	'guest_network_dhcp_range',
	'guest_network_dhcp_dns_server',
	'guest_network_isolate_cidrs',
];

export function fieldLabel(key: string): string {
	return FIELD_LABELS[key] || key.replace(/_/g, ' ');
}

export function settingsFor(all: SettingOverride[], keys: string[]): SettingOverride[] {
	const byKey = new Map(all.map((s) => [s.key, s]));
	return keys.map((k) => byKey.get(k)).filter((s): s is SettingOverride => s !== undefined);
}

/** Everything this page has no home for - rendered rather than dropped. */
export function unplacedSettings(all: SettingOverride[], subsystemNames: string[]): SettingOverride[] {
	const placed = new Set<string>();
	for (const name of subsystemNames) {
		for (const key of SUBSYSTEM_SETTINGS[name] || []) placed.add(key);
	}
	for (const group of EXTRA_GROUPS) {
		for (const key of group.keys) placed.add(key);
	}
	// The guest-network settings live on their own card, which renders them next
	// to the survey and the plan they describe rather than in this list.
	for (const key of GUEST_NETWORK_KEYS) placed.add(key);
	return all.filter((s) => !placed.has(s.key));
}

/**
 * What the UI promises about a save. `hot_reloadable` is the server's claim
 * that every consumer re-reads the value at use time; anything else is labelled
 * honestly rather than implied to be live.
 */
export function reloadLabel(setting: Pick<SettingOverride, 'hot_reloadable'>): string {
	return setting.hot_reloadable ? 'takes effect on the next cycle' : 'restart required';
}

/**
 * Why a field is read-only - and how to stop it being read-only (#549 F7).
 *
 * Naming the variable explains the lock; it does not explain the way out, and
 * an operator who wants to manage the value from the product has to be told
 * that the environment is what stands in the way and that removing it hands
 * control back here. Saying only half of that is how "read-only" reads as
 * "not configurable".
 */
export function envLockNote(setting: Pick<SettingOverride, 'env_var'>): string {
	return (
		`Set by ${setting.env_var} in the environment, which wins over anything saved here - ` +
		`to manage it on this card instead, remove ${setting.env_var} from the server's ` +
		`environment (.env) and restart.`
	);
}
