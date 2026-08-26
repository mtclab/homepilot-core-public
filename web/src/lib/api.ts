import { get, writable } from 'svelte/store';
import { base } from '$app/paths';
import { getApiBase } from './apiBase';

// A failed request carries its HTTP status and a human-readable message —
// never the raw `401: {"detail":...}` JSON the API returns. `.detail` keeps
// the server's text for callers that want it.
export class ApiError extends Error {
	status: number;
	detail: string;
	constructor(status: number, body: string) {
		let detail = body;
		try {
			const parsed = JSON.parse(body);
			if (parsed && parsed.detail !== undefined && parsed.detail !== null) {
				// `detail` may be a string OR a structured object (e.g. a revoke
				// conflict sends `{"reason": ...}`). Never let raw JSON reach a toast.
				detail = ApiError.detailToString(parsed.detail);
			}
		} catch {
			// non-JSON body: keep as-is
		}
		super(ApiError.humanize(status, detail));
		this.status = status;
		this.detail = detail;
		this.name = 'ApiError';
	}
	// Collapse a server `detail` (string or object) to a single human line.
	static detailToString(d: unknown): string {
		if (typeof d === 'string') return d;
		if (d && typeof d === 'object') {
			const o = d as Record<string, unknown>;
			for (const k of ['reason', 'message', 'error', 'detail']) {
				if (typeof o[k] === 'string') return o[k] as string;
			}
			try {
				return JSON.stringify(d);
			} catch {
				return String(d);
			}
		}
		return String(d);
	}
	static humanize(status: number, detail: string): string {
		switch (status) {
			case 401:
				return 'Invalid or expired token.';
			case 403:
				return "You don't have permission for that.";
			case 404:
				return 'Not found.';
			case 409:
				return detail || 'That conflicts with the current state — refresh and try again.';
			case 429:
				return 'Too many requests — wait a moment and try again.';
			default:
				if (status >= 500) return 'The server hit an error. Try again shortly.';
				return detail || `Request failed (${status}).`;
		}
	}
}

const _tokenStore = writable('');
export const sessionStore = writable<MeInfo | null>(null);

function getCsrfToken(): string {
	if (typeof document === 'undefined') return '';
	const m = document.cookie.match(/(?:^|;\s*)hp_csrf=([^;]+)/);
	return m ? decodeURIComponent(m[1]) : '';
}

// Guards against firing several redirects while one is already in flight.
let _redirectingToLogin = false;

// A 401 mid-session means the credential is stale. Drop local session state so
// the UI stops replaying a dead token, then bounce to login once (preserving
// where the user was). Skipped for the auth probes themselves, which would
// otherwise loop the login page.
function handleUnauthorized(path: string): void {
	_tokenStore.set('');
	sessionStore.set(null);
	if (typeof window === 'undefined') return;
	if (path.startsWith('/auth/me') || path.startsWith('/auth/login')) return;
	const loginPath = `${base}/login`;
	if (window.location.pathname === loginPath || _redirectingToLogin) return;
	_redirectingToLogin = true;
	const returnTo = encodeURIComponent(window.location.pathname + window.location.search);
	window.location.assign(`${loginPath}?returnTo=${returnTo}`);
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

	// Resolved per request: the operator can change the base in Settings without
	// a page reload.
	const res = await fetch(`${getApiBase()}${path}`, {
		...init,
		credentials: 'include',
		headers,
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		if (res.status === 401) handleUnauthorized(path);
		throw new ApiError(res.status, text);
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

export type TaskStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface Task {
	id: string;
	// null for artifactless tasks (provision creates infrastructure, not intent)
	artifact_id: string | null;
	action: 'apply' | 'revoke' | 'replay' | 'provision' | 'install_agent';
	status: TaskStatus;
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
	state: Record<string, unknown>;
	connected_at: string;
	last_heartbeat: string;
	stale_seconds?: number;
	connected?: boolean;
	disconnected_at?: string | null;
	/**
	 * Why this agent is not here (#430). Null on a live agent - the hub clears it
	 * on a successful register - and on any agent that has never been refused.
	 */
	last_error?: string | null;
	last_error_at?: string | null;
}

export interface AgentInstallEligibility {
	host_id: string;
	hostname: string | null;
	eligible: boolean;
	// A stable code (no_guest_agent, not_running, already_enrolled, …) plus the
	// message that says what to do about it.
	reason: string | null;
	message: string;
	in_flight: boolean;
}

export interface EnrolmentWindow {
	open: boolean;
	expires_at: string | null;
	seconds_remaining: number;
	// An install with no agents at all enrols its first host with or without a
	// window, so "closed" alone would misdescribe what happens next.
	fleet_empty: boolean;
}

export interface OnboardingStep {
	key: string;
	title: string;
	detail: string;
	href: string;
	done: boolean;
}

export interface DashboardSummary {
	/**
	 * The first-run path (#445 A7). Every step's `done` is derived from the
	 * estate itself, never from a stored "you did the setup" flag - a checklist
	 * that ticks itself off from anything else is a tutorial that lies.
	 */
	onboarding: { steps: OnboardingStep[]; complete: boolean };
	inventory: {
		total: number;
		managed: number;
		uncovered: number;
		coverage_pct: number;
		by_status: Record<string, number>;
		by_role: Record<string, number>;
		by_type: Record<string, number>;
	};
	drift: {
		total: number;
		drifted: number;
		in_spec: number;
		/** Checks that could not establish anything - never counted as healthy (#425). */
		unknown: number;
		checked: number;
		in_spec_pct: number;
	};
	artifacts: Record<string, number>;
	tasks: Record<string, number>;
	agents: { known: number; connected: number };
	metrics: { firing_alerts: number; retention_days: number };
}

// --- Native metrics (ADR-004 S5) ---
export interface MetricPoint {
	ts: number;
	value: number;
}

export interface MetricSeries {
	hostname: string;
	metric: string;
	since: number;
	points: MetricPoint[];
	// True when the window held more points than `max_points`; the OLDEST were
	// left out. Nothing is averaged - every point returned was really reported.
	truncated: boolean;
	max_points: number;
}

export interface LatestMetric {
	metric: string;
	ts: number;
	value: number;
}

export type AlertComparison = 'gt' | 'gte' | 'lt' | 'lte';

export interface AlertRule {
	id: string;
	name: string;
	host_filter: string;
	metric: string;
	comparison: AlertComparison;
	threshold: number;
	// The duration condition: the rule fires only when it held this long.
	for_seconds: number;
	enabled: number;
	created_at: string;
	updated_at: string;
}

export interface FiringAlert {
	rule_id: string;
	hostname: string;
	firing_since: string;
	last_value: number;
	last_eval: string;
	name: string;
	metric: string;
	comparison: AlertComparison;
	threshold: number;
	for_seconds: number;
}

export interface HealthInfo {
	status: string;
	version?: string;
	checks?: { [key: string]: string };
	[key: string]: string | { [key: string]: string } | undefined;
}

// One optional subsystem in the startup self-check (ADR-004 S6). `state` splits
// "off by choice" from "configured but unreachable" because those need opposite
// actions from an operator; `consequence` says what it costs in plain words.
export type SelfcheckState = 'off' | 'ok' | 'unreachable' | 'unknown';

export interface SelfcheckSubsystem {
	name: string;
	/** Optional server-sent display name; the UI falls back to its own list. */
	label?: string;
	configured: boolean;
	state: SelfcheckState;
	target: string;
	consequence: string;
}

export interface SelfcheckReport {
	timeout_seconds: number;
	counts: Record<SelfcheckState, number>;
	subsystems: SelfcheckSubsystem[];
}

// One operator-editable setting (#553 C2). `source` is binding: `env` means the
// environment decides it and the server will REFUSE a write; `db` means a saved
// value is in force; `default` means the code default is.
export type SettingSource = 'env' | 'db' | 'default';

export interface SettingOverride {
	key: string;
	value: string | number;
	source: SettingSource;
	type: 'str' | 'int';
	hot_reloadable: boolean;
	description: string;
	env_var: string;
	editable: boolean;
	/** #553 C3: this value can be tried against the live cluster before saving. */
	probeable?: boolean;
}

export interface SettingProbe {
	key: string;
	ok: boolean;
	reachable: boolean;
	detail: string;
}

export interface ArtifactDetail {
	frontmatter: Artifact;
	body: string;
	active_task: ActiveTask | null;
	// The per-artifact approval code, present only while PROPOSED. A human reads
	// it here and relays it to the assistant to approve over MCP (the assistant
	// cannot see it there). `approval_locked` is true once too many wrong codes
	// were relayed; an operator clears it with resetApprovalCode.
	approval_code?: string | null;
	approval_locked?: boolean;
}

/** What a person fills in to propose an artifact; the server adds the rest. */
export interface ArtifactProposal {
	kind: string;
	intent: string;
	body: string;
	idempotence?: string;
	target?: {
		kind: string;
		host?: string;
		vmid?: number;
		node?: string;
		service?: string;
		network?: string;
	};
	tags?: string[];
}

/** One thing the plan checked on the host, with what is there now. */
export interface ArtifactPlanItem {
	kind: 'package' | 'service' | 'config';
	id: string;
	name: string;
	desired: string;
	observed: string;
	changes: boolean;
	log: string;
}

export interface ArtifactPolicy {
	id?: number | string;
	title: string;
	content: string;
	target?: string | null;
}

export interface ArtifactPlan {
	artifact_id: string;
	host: string;
	kind: string;
	items: ArtifactPlanItem[];
	change_count: number;
	in_spec: boolean;
	summary: string;
	/**
	 * The operator's own recorded rules for this host (#429). Reviewing is meant
	 * to be an informed decision, and this is the half the plan cannot supply.
	 */
	policies?: ArtifactPolicy[];
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
	proxmox_id?: number | null;
	os_info?: string | null;
	cpu_cores?: number | null;
	memory_mb?: number | null;
	disk_gb?: number | null;
	/**
	 * When the hypervisor stopped reporting this host (#445 A5). Null for a host
	 * Proxmox still sees, and for every manually added host - Proxmox never
	 * looked for those, so it has no standing to call them gone.
	 */
	absent_since?: string | null;
	/** The agent enrolled on this machine (#514 S1) - the live channel. */
	agent_id?: string | null;
	agent_connected?: boolean;
	agent_version?: string | null;
}

/** The host page's Agent section (#514 S2): the channel, and why it last broke. */
export interface AgentOnHost {
	agent_id: string;
	connected: boolean;
	version?: string | null;
	arch?: string | null;
	runtime?: string | null;
	first_seen?: string | null;
	connected_at?: string | null;
	last_heartbeat?: string | null;
	disconnected_at?: string | null;
	last_error?: string | null;
	last_error_at?: string | null;
	credential_set_at?: string | null;
	revoked_at?: string | null;
}

export interface DriftCheck {
	artifact_id: string;
	drifted: boolean;
	checked_at: string;
	details_json: string | null;
	/**
	 * What the check actually established (#425). `drifted: false` used to cover
	 * both "I looked and it matches" and "I could not look", and the UI painted
	 * both green. `unknown` is the honest third answer.
	 */
	state?: 'in_spec' | 'drifted' | 'unknown';
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


/** Operator view of the guest world (#442 G3). */
export interface GuestOverview {
	guests: Array<{
		cn: string;
		usage: { vms: number; cores: number; memory_mb: number; disk_gb: number };
		limits: { vms: number | null; cores: number | null; memory_mb: number | null; disk_gb: number | null } | null;
	}>;
	invites: Array<{
		id: string;
		prefix: string;
		cn: string;
		state: string;
		caps: { template_vmid: number; node: string; cores: number; memory_mb: number; disk_gb: number };
		expires_at: string;
		created_at: string;
	}>;
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
	// Short token prefix, e.g. "hp_ab12". Optional: older servers omit it, so
	// callers must fall back rather than render a literal "undefined".
	prefix?: string;
	// Normalized capability list, e.g. ["read","write"] or ["read","write","admin"].
	// The UI gates write/admin controls off this — never off the raw `scope`
	// string. Optional so older servers that omit it degrade gracefully.
	capabilities?: string[];
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

// First-run claim (#458). `/claim/status` says whether the instance has ever
// been claimed and — only while it is unclaimed — whether THIS caller needs the
// claim code. `code_required` describes the caller's own source address (it is
// false on the local network, true when the instance is reached from outside),
// so it discloses nothing about the instance itself.
export interface ClaimStatus {
	state: 'unclaimed' | 'claimed';
	code_required?: boolean;
}

export interface ClaimIn {
	// Omitted on the local network; required when `code_required` is true.
	code?: string;
	label?: string;
	proxmox_host?: string;
	proxmox_port?: number;
	proxmox_token?: string;
	proxmox_verify_ssl?: boolean;
}

export interface ClaimResult {
	// The admin token, returned exactly once. Nothing re-issues it.
	token: string;
	scope: string;
	proxmox_configured: boolean;
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
	listArtifacts(params: { status?: string; kind?: string; q?: string; limit?: number } = {}) {
		return req<{ items: Artifact[]; total: number }>('/artifacts' + qs(params));
	},
	getArtifact(id: string) {
		return req<ArtifactDetail>(`/artifacts/${id}`);
	},
	/**
	 * Propose a new artifact (#445 A2).
	 *
	 * `id` and `produced_by` are deliberately NOT sent: the server derives them,
	 * so every client gets the same identity rules and `user` comes from the
	 * authenticated token rather than from whatever the browser claims.
	 */
	proposeArtifact(spec: ArtifactProposal) {
		return req<{ id: string }>('/artifacts', {
			method: 'POST',
			body: JSON.stringify(spec),
		});
	},
	/**
	 * What applying this artifact would change ON THE HOST (#445 A1).
	 *
	 * Not to be confused with the preview endpoint, which diffs the artifact
	 * FILE. Read-only: it runs the same probe drift uses, so opening an approval
	 * screen cannot alter anything.
	 */
	planArtifact(id: string) {
		return req<ArtifactPlan>(`/artifacts/${id}/plan`, { method: 'POST' });
	},
	approveArtifact(id: string, user = 'web') {
		return req<{ id: string; status: string }>(`/artifacts/${id}/approve`, {
			method: 'POST',
			body: JSON.stringify({ user }),
		});
	},
	resetApprovalCode(id: string) {
		return req<{ id: string; locked: boolean }>(`/artifacts/${id}/approval-code/reset`, {
			method: 'POST',
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
	// Omit artifactId for the system-wide view (all tasks, newest first); pass it
	// to scope to a single artifact.
	listTasks(artifactId?: string, limit = 50, offset = 0) {
		return req<{ items: Task[]; total: number }>('/tasks' + qs({ artifact_id: artifactId, limit, offset }));
	},
	// Cancels an in-flight apply/revoke; a no-op returning the current status for
	// an already-finished task. Returns the task's post-cancel record.
	cancelTask(taskId: string) {
		return req<Task>(`/tasks/${taskId}/cancel`, { method: 'POST' });
	},

	// --- Drift ---
	getDriftStatus(params: { refresh?: boolean; artifact_id?: string; drifted?: boolean; limit?: number } = {}) {
		return req<{ items: DriftCheck[]; total: number }>('/artifacts/drift' + qs(params as Record<string, string | number | boolean | undefined>));
	},
	recheckDrift(artifactId: string) {
		return req<{ items: DriftCheck[]; total: number }>('/artifacts/drift?refresh=true&artifact_id=' + encodeURIComponent(artifactId));
	},

	// --- Audit ---
	listAudit(params: { action?: string; artifact_id?: string; source?: string; target_host?: string; q?: string; limit?: number; offset?: number } = {}) {
		return req<{ items: AuditEntry[]; total: number }>('/audit' + qs(params as Record<string, string | number | boolean | undefined>));
	},

	// --- Inventory ---
	listInventory(params: { role?: string; status?: string; managed?: boolean; source?: string; import_state?: string; pve_status?: string; q?: string; limit?: number; offset?: number } = {}) {
		return req<{ items: Host[]; total: number }>('/inventory' + qs(params));
	},
	/**
	 * Add a host Proxmox has never heard of (#445 A5) - the NAS, the router, the
	 * Pi. Recorded as `source: "manual"`, which is also what stops a sync from
	 * ever declaring it absent.
	 */
	addHost(body: {
		hostname: string;
		ip_address?: string;
		role?: string;
		host_type?: string;
		description?: string;
		tags?: string;
		fqdn?: string;
	}) {
		return req<Host>('/inventory', { method: 'POST', body: JSON.stringify(body) });
	},
	/** Remove a host and its services/observation note. 409 while Proxmox still reports it. */
	forgetHost(hostId: string) {
		return req<{ id: string; forgotten: boolean }>(`/inventory/${hostId}`, { method: 'DELETE' });
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
		return req<Host & { services: Service[]; agent?: AgentOnHost }>(`/inventory/${id}`);
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

	// --- First-run claim ---
	claimStatus() {
		return req<ClaimStatus>('/claim/status');
	},
	claimInstance(body: ClaimIn) {
		return req<ClaimResult>('/claim', { method: 'POST', body: JSON.stringify(body) });
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
	/**
	 * Forget a decommissioned agent (#415).
	 *
	 * Removes the persisted row AND revokes its per-agent credential - that row
	 * IS the credential store, so a scrapped host whose record survives can still
	 * authenticate. Refused with 409 while the agent is connected.
	 */
	/** Revoke an agent's credential AND close its live channel (#430). */
	revokeAgent(agentId: string) {
		return req<{ agent_id: string; revoked: boolean; channel_closed: boolean }>(
			`/agents/${agentId}/revoke`,
			{ method: 'POST' }
		);
	},
	forgetAgent(agentId: string) {
		return req<{ agent_id: string; forgotten: boolean }>(`/agents/${agentId}`, {
			method: 'DELETE',
		});
	},
	getBootstrapToken() {
		return req<{
			bootstrap_token: string;
			hub_host: string;
			hub_port: number;
			hub_tls: boolean;
			hub_cert_sha256: string;
		}>('/agents/bootstrap', { method: 'POST' });
	},
	// The enrolment window (#537): while it is closed, the shared hub token
	// cannot enrol a host this install has never seen.
	getEnrolmentWindow() {
		return req<EnrolmentWindow>('/agents/enrolment-window');
	},
	openEnrolmentWindow(minutes: number) {
		return req<EnrolmentWindow>('/agents/enrolment-window', {
			method: 'POST',
			body: JSON.stringify({ minutes }),
		});
	},
	closeEnrolmentWindow() {
		return req<EnrolmentWindow>('/agents/enrolment-window', { method: 'DELETE' });
	},
	getHubToken() {
		return req<{
			auth_token: string;
			hub_host: string;
			hub_port: number;
			hub_tls: boolean;
			hub_cert_sha256: string;
		}>('/agents/token');
	},
	// Whether HomePilot can install the agent into this guest over
	// qemu-guest-agent, and when it cannot, the reason to show instead of the
	// button.
	agentInstallEligibility(hostId: string) {
		return req<AgentInstallEligibility>(`/agents/install/${hostId}`);
	},
	installAgent(hostId: string) {
		return req<{ task_id: string; status: string; host_id: string }>('/agents/install', {
			method: 'POST',
			body: JSON.stringify({ host_id: hostId }),
		});
	},
	async getHealth(): Promise<HealthInfo> {
		// `/health` returns 503 when a dependency is degraded — exactly when the
		// panel matters most. Fetch directly and parse the body regardless of
		// status so the individual checks render instead of going blank. Only a
		// hard network/JSON failure rejects.
		const memToken = get(_tokenStore);
		const headers: Record<string, string> = {};
		if (memToken) headers['Authorization'] = `Bearer ${memToken}`;
		const res = await fetch(`${getApiBase()}/health`, { credentials: 'include', headers });
		return res.json() as Promise<HealthInfo>;
	},
	getSelfcheck() {
		return req<SelfcheckReport>('/admin/selfcheck');
	},
	getDashboard() {
		return req<DashboardSummary>('/dashboard/summary');
	},
	getUiConfig() {
		return req<{ metrics_retention_days: number }>('/dashboard/config');
	},

	// --- Metrics ---
	getHostSeries(hostname: string, metric: string, hours = 1) {
		return req<MetricSeries>(`/monitoring/hosts/${encodeURIComponent(hostname)}/series` + qs({ metric, hours }));
	},
	getHostLatest(hostname: string) {
		return req<{ hostname: string; metrics: LatestMetric[] }>(`/monitoring/hosts/${encodeURIComponent(hostname)}/latest`);
	},
	listAlertRules() {
		return req<{ items: AlertRule[]; total: number }>('/monitoring/rules');
	},
	createAlertRule(rule: {
		name: string;
		metric: string;
		comparison: AlertComparison;
		threshold: number;
		for_seconds?: number;
		host_filter?: string;
	}) {
		return req<AlertRule>('/monitoring/rules', { method: 'POST', body: JSON.stringify(rule) });
	},
	setAlertRuleEnabled(ruleId: string, enabled: boolean) {
		return req<AlertRule>(`/monitoring/rules/${encodeURIComponent(ruleId)}`, {
			method: 'PATCH',
			body: JSON.stringify({ enabled }),
		});
	},
	deleteAlertRule(ruleId: string) {
		return req<{ id: string; deleted: boolean }>(`/monitoring/rules/${encodeURIComponent(ruleId)}`, { method: 'DELETE' });
	},
	listFiringAlerts() {
		return req<{ items: FiringAlert[]; total: number }>('/monitoring/alerts');
	},

	// --- Guests (#442 G3) ---
	getGuests() {
		return req<GuestOverview>('/admin/guests');
	},
	// `template_vmid` and `node` are nullable because they are OPTIONAL server-side
	// since #553 C3: omitting them means "use this instance's provisioning
	// defaults", and the response echoes back which ones the invite actually got.
	mintGuestInvite(body: { cn: string; template_vmid: number | null; node: string | null; cores: number; memory_mb: number; disk_gb: number; ttl_days: number }) {
		return req<{
			id: string;
			token: string;
			cn: string;
			caps?: { node: string; template_vmid: number; pool: string | null; ipconfig0: string | null };
		}>('/admin/guests/invites', {
			method: 'POST',
			body: JSON.stringify(body),
		});
	},
	revokeGuestInvite(prefix: string) {
		return req<{ prefix: string; revoked: boolean }>(`/admin/guests/invites/${encodeURIComponent(prefix)}/revoke`, { method: 'POST' });
	},
	setGuestQuota(body: { cn: string; max_vms: number | null; max_cores: number | null; max_memory_mb: number | null; max_disk_gb: number | null }) {
		return req<unknown>('/admin/guests/quota', { method: 'POST', body: JSON.stringify(body) });
	},

	// --- Operator settings (#553 C2) ---
	listSettingOverrides() {
		return req<{ settings: SettingOverride[] }>('/admin/settings/overrides');
	},
	saveSettingOverride(key: string, value: string | number) {
		return req<{ status: string; key: string; value: string | number; source: SettingSource }>(
			`/admin/settings/overrides/${encodeURIComponent(key)}`,
			{ method: 'PUT', body: JSON.stringify({ value }) },
		);
	},
	probeSettingOverride(key: string, value: string | number) {
		return req<SettingProbe>(`/admin/settings/overrides/${encodeURIComponent(key)}/probe`, {
			method: 'POST',
			body: JSON.stringify({ value }),
		});
	},
	clearSettingOverride(key: string) {
		return req<{ status: string; key: string; value: string | number; source: SettingSource }>(
			`/admin/settings/overrides/${encodeURIComponent(key)}`,
			{ method: 'DELETE' },
		);
	},

	getProxmoxSettings() {
		return req<ProxmoxSettings>('/admin/settings/proxmox');
	},
	saveProxmoxSettings(data: ProxmoxConfigIn) {
		return req<{ status: string; message?: string; reloaded: string[]; host: string; port: number; verify_ssl: boolean; token_configured: boolean; write_token_configured: boolean }>('/admin/settings/proxmox', {
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
// Link to a host's own metrics inside HomePilot (#514 S2): the host page is
// where a machine's stats live now. Takes the host ID when the caller has it;
// hostname fallback resolves through the list for callers that don't.
export function hostMetricsUrl(base: string, hostname: string, hostId?: string): string {
	// `?tab=metrics` since #549 F3: the host page's sections became tabs, so a
	// link that promises metrics has to name the metrics tab or it lands on
	// Overview and the promise is broken.
	if (hostId) return `${base}/hosts/${encodeURIComponent(hostId)}?tab=metrics`;
	if (!hostname) return '';
	// No id in hand: land on the filtered fleet list (S4 renames it to Hosts).
	return `${base}/inventory?q=${encodeURIComponent(hostname)}`;
}
