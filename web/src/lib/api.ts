import { get, writable } from 'svelte/store';

const API_BASE = import.meta.env.VITE_API_BASE || '';

const _tokenStore = writable('');
export const sessionStore = writable<MeInfo | null>(null);

function getCsrfToken(): string {
	if (typeof document === 'undefined') return '';
	const m = document.cookie.match(/(?:^|;\s*)hp_csrf=([^;]+)/);
	return m ? decodeURIComponent(m[1]) : '';
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
	const method = ((init.method ?? 'GET') as string).toUpperCase();
	const isMutation = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);
	const memToken = get(_tokenStore);
	const csrfToken = getCsrfToken();

	const headers: Record<string, string> = {
		'Content-Type': 'application/json',
		...(init.headers as Record<string, string>),
	};

	if (memToken) {
		headers['Authorization'] = `Bearer ${memToken}`;
	} else if (isMutation) {
		headers['X-Requested-With'] = 'XMLHttpRequest';
		if (csrfToken) {
			headers['X-CSRF-Token'] = csrfToken;
		}
	}

	const res = await fetch(`${API_BASE}${path}`, {
		...init,
		credentials: 'include',
		headers,
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`${res.status}: ${text}`);
	}
	if (res.status === 204) return undefined as T;
	return res.json() as Promise<T>;
}

function qs(params: Record<string, string | number | boolean | undefined>): string {
	const p = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) {
		if (v !== undefined && v !== '') p.set(k, String(v));
	}
	const s = p.toString();
	return s ? `?${s}` : '';
}

export interface Artifact {
	id: string;
	status: string;
	kind: string;
	intent: string;
	created_at: string;
	created_by?: string;
	target?: Record<string, string>;
	replay_safe?: boolean;
	tags?: string[];
	version?: string;
}

export interface Task {
	id: string;
	artifact_id: string;
	action: 'apply' | 'revoke' | 'replay';
	status: 'pending' | 'running' | 'succeeded' | 'failed';
	result_json: string | null;
	created_at: string;
	finished_at: string | null;
	error: string | null;
}

export interface ActiveTask {
	id: string;
	status: string;
	action: string;
}

export interface AgentInfo {
	agent_id: string;
	hostname: string;
	system_info: Record<string, unknown>;
	state: string;
	connected_at: string;
	last_heartbeat: string;
}

export interface AgentDetail {
	agent_id: string;
	hostname: string;
	system_info: Record<string, unknown>;
	state: string;
	connected_at: string;
	last_heartbeat: string;
}

export interface HealthInfo {
	status: string;
	version?: string;
	checks?: { [key: string]: string };
	[key: string]: string | { [key: string]: string } | undefined;
}

export interface ArtifactDetail {
	frontmatter: Artifact;
	body: string;
	active_task: ActiveTask | null;
}

export interface Host {
	id: string;
	hostname: string;
	node?: string;
	ip_address?: string;
	role?: string;
	status?: string;
	managed?: number;
	tags?: string;
	host_type?: string;
	fqdn?: string;
	pve_status?: string;
	source?: string;
	description?: string;
	artifact_id?: string;
	import_state?: string;
	role_source?: string;
	ip_source?: string;
}

export interface DriftCheck {
	artifact_id: string;
	drifted: boolean;
	checked_at: string;
	details_json: string | null;
}

export interface AuditEntry {
	id: number;
	timestamp: string;
	user_id: string;
	source: string;
	action: string;
	artifact_id: string | null;
	target_host: string | null;
	target_service: string | null;
	command: string | null;
	exit_code: number | null;
	snapshot_id: string | null;
	duration_ms: number | null;
	details_json: string | null;
}

export interface HostDoc {
	target: string;
	hosts: Host[];
	services: Service[];
	kb_entries: KBEntry[];
	artifact_history: Artifact[];
}

export interface Service {
	id: string;
	host_id: string;
	name: string;
	port?: number;
	status?: string;
}

export interface KBEntry {
	id: number;
	source: string;
	kind: string;
	target?: string;
	title?: string;
	content: string;
	embedded_at?: string;
}

export interface TokenInfo {
	id: string;
	prefix: string;
	scope: string | null;
	role: string | null;
	label: string | null;
	token_type: string;
	created_at: string;
	last_used_at: string | null;
	expires_at: string | null;
}

export interface MeInfo {
	authenticated: boolean;
	token_label: string;
	scope: string | null;
	role: string | null;
	prefix: string;
}

export interface ProxmoxSettings {
	host: string;
	port: number;
	verify_ssl: boolean;
	token_configured: boolean;
	token_source: string;
	write_token_configured: boolean;
	write_token_source: string;
	write_token_is_separate: boolean;
	connection_status: string;
}

export interface ProxmoxConfigIn {
	host?: string | null;
	port?: number | null;
	verify_ssl?: boolean | null;
	token?: string | null;
	write_token?: string | null;
}

export const api = {
	// --- Artifacts ---
	listArtifacts(params: { status?: string; kind?: string; limit?: number } = {}) {
		return req<{ items: Artifact[]; total: number }>('/artifacts' + qs(params));
	},
	getArtifact(id: string) {
		return req<ArtifactDetail>(`/artifacts/${id}`);
	},
	approveArtifact(id: string, user = 'web') {
		return req<{ id: string; status: string }>(`/artifacts/${id}/approve`, {
			method: 'POST',
			body: JSON.stringify({ user }),
		});
	},
	rejectArtifact(id: string, user = 'web', reason?: string) {
		return req<{ id: string; status: string }>(`/artifacts/${id}/reject`, {
			method: 'POST',
			body: JSON.stringify({ user, reason }),
		});
	},
	applyArtifact(id: string, approved_by = 'web') {
		return req<{ task_id: string; artifact_id: string; status: string; action: string }>(`/artifacts/${id}/apply`, {
			method: 'POST',
			body: JSON.stringify({ approved_by }),
		});
	},
	revokeArtifact(id: string, user = 'web', reason?: string) {
		return req<{ task_id: string; artifact_id: string; status: string; action: string }>(`/artifacts/${id}`, {
			method: 'DELETE',
			body: JSON.stringify({ user, reason }),
		});
	},

	getTask(taskId: string) {
		return req<Task>(`/tasks/${taskId}`);
	},
	listTasks(artifactId: string, limit = 50, offset = 0) {
		return req<{ items: Task[]; total: number }>('/tasks' + qs({ artifact_id: artifactId, limit, offset }));
	},

	// --- Drift ---
	getDriftStatus(params: { refresh?: boolean; artifact_id?: string; drifted?: boolean; limit?: number } = {}) {
		return req<{ items: DriftCheck[]; total: number }>('/artifacts/drift' + qs(params as Record<string, string | number | boolean | undefined>));
	},
	recheckDrift(artifactId: string) {
		return req<{ items: DriftCheck[]; total: number }>('/artifacts/drift?refresh=true&artifact_id=' + encodeURIComponent(artifactId));
	},

	// --- Audit ---
	listAudit(params: { action?: string; artifact_id?: string; source?: string; limit?: number; offset?: number } = {}) {
		return req<{ items: AuditEntry[]; total: number }>('/audit' + qs(params as Record<string, string | number | boolean | undefined>));
	},

	// --- Inventory ---
	listInventory(params: { role?: string; status?: string; managed?: boolean; source?: string; import_state?: string; pve_status?: string } = {}) {
		return req<{ items: Host[]; total: number }>('/inventory' + qs(params));
	},
	refreshInventory() {
		return req<{ hosts: number; services: number }>('/inventory/refresh', { method: 'POST' });
	},
	enrichInventory(host_ids?: string[], scope?: string) {
		return req<{ enriched: number; failed: number; skipped: number }>('/inventory/enrich', {
			method: 'POST',
			body: JSON.stringify({ host_ids, scope }),
		});
	},
	adoptHost(id: string) {
		return req<Host>(`/inventory/${id}/adopt`, { method: 'POST' });
	},
	ignoreHost(id: string) {
		return req<Host>(`/inventory/${id}/ignore`, { method: 'POST' });
	},
	bulkInventory(action: string, hostIds: string[]) {
		return req<{ succeeded: number; failed: number }>('/inventory/bulk', {
			method: 'POST',
			body: JSON.stringify({ action, host_ids: hostIds }),
		});
	},
	getHost(id: string) {
		return req<Host & { services: Service[] }>(`/inventory/${id}`);
	},
	getHostDoc(id: string) {
		return req<HostDoc>(`/inventory/${id}/doc`);
	},
	updateHost(id: string, data: { role?: string; ip_address?: string; description?: string; tags?: string; managed?: boolean; import_state?: string; status?: string }) {
		return req<Host>(`/inventory/${id}`, {
			method: 'PATCH',
			body: JSON.stringify(data),
		});
	},

	// --- KB ---
	searchKB(q: string, kind?: string, limit = 10) {
		return req<{ results: KBEntry[]; total: number }>('/kb/search' + qs({ q, kind, limit }));
	},
	listKB(params: { kind?: string; target?: string } = {}) {
		return req<{ items: KBEntry[]; total: number }>('/kb' + qs(params));
	},
	getKBDoc(docId: number) {
		return req<KBEntry>(`/kb/${docId}`);
	},
	updateKBDoc(docId: number, data: { title?: string; content?: string; kind?: string; target?: string }) {
		return req<KBEntry>(`/kb/${docId}`, {
			method: 'PUT',
			body: JSON.stringify(data),
		});
	},
	deleteKBDoc(docId: number) {
		return req<void>(`/kb/${docId}`, { method: 'DELETE' });
	},
	createKBNote(target: string, kind: string, content: string, supersedes?: string[]) {
		return req<{ id: string }>('/kb/notes', {
			method: 'POST',
			body: JSON.stringify({ target, kind, content, supersedes }),
		});
	},

	// --- Auth ---
	me() {
		return req<MeInfo>('/auth/me');
	},
	login(token: string) {
		return req<{ status: string }>('/auth/login', {
			method: 'POST',
			body: JSON.stringify({ token }),
		});
	},
	logout() {
		return req<{ status: string }>('/auth/logout', { method: 'POST' });
	},
	listTokens() {
		return req<{ items: TokenInfo[]; total: number }>('/auth/tokens');
	},
	createToken(label: string, scope: string, adminSecret: string) {
		return req<{ token: string; scope: string }>('/auth/tokens', {
			method: 'POST',
			body: JSON.stringify({ label, scope }),
			headers: { 'x-hp-admin-secret': adminSecret },
		});
	},
	revokeToken(prefix: string) {
		return req<void>(`/auth/tokens/${prefix}`, { method: 'DELETE' });
	},

	// --- Agents ---
	listAgents() {
		return req<AgentInfo[]>('/agents/');
	},
	getBootstrapToken() {
		return req<{ bootstrap_token: string; hub_host: string; hub_port: number }>('/agents/bootstrap');
	},
	getHubToken() {
		return req<{ auth_token: string; hub_host: string; hub_port: number }>('/agents/token');
	},
	getAgent(agentId: string) {
		return req<AgentDetail>(`/agents/${agentId}`);
	},
	getHealth() {
		return req<HealthInfo>('/health');
	},

	getProxmoxSettings() {
		return req<ProxmoxSettings>('/admin/settings/proxmox');
	},
	saveProxmoxSettings(data: ProxmoxConfigIn) {
		return req<{ status: string; reloaded: string[]; host: string; port: number; verify_ssl: boolean; token_configured: boolean; write_token_configured: boolean }>('/admin/settings/proxmox', {
			method: 'PUT',
			body: JSON.stringify(data),
		});
	},
	testProxmoxConnection(data: ProxmoxConfigIn) {
		return req<{ status: string; message: string; version?: Record<string, unknown> }>('/admin/settings/proxmox/test', {
			method: 'POST',
			body: JSON.stringify(data),
		});
	},
	reloadSecrets() {
		return req<{ status: string; reloaded: string[]; timestamp: string }>('/admin/reload-secrets', { method: 'POST' });
	},
};

export function setToken(t: string) {
	_tokenStore.set(t);
}

export function getToken(): string {
	return get(_tokenStore);
}

export function hasCookieSession(): boolean {
	return getCsrfToken() !== '';
}

export async function hasSession(): Promise<boolean> {
	if (hasCookieSession()) return true;
	try {
		await api.me();
		return true;
	} catch {
		return false;
	}
}

export async function refreshSession(): Promise<MeInfo | null> {
	try {
		const me = await api.me();
		sessionStore.set(me);
		return me;
	} catch {
		sessionStore.set(null);
		return null;
	}
}