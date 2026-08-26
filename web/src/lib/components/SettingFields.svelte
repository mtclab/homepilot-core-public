<script lang="ts">
	// Editable operator settings, in place on the card whose subsystem they
	// configure (#553 C2).
	//
	// Three things every field must say, because the server's precedence is
	// binding and an operator has to be able to predict it: what the value is,
	// WHERE it came from, and whether saving takes effect now. A field the
	// environment decides renders read-only and names the variable - offering an
	// input the server would answer 409 to is the "saved!" lie this slice exists
	// to remove, and when the server does answer 409 its sentence is shown
	// verbatim rather than paraphrased into a toast.
	// A probeable setting (#553 C3) also gets a Test: the cluster's answer BEFORE
	// a save, in the cluster's own words, so an operator can find out that pve1
	// has no vmbr7 without first being told "no" by a 422.
	import { api, ApiError, type SettingOverride, type SettingProbe } from '$lib/api';
	import { envLockNote, fieldLabel, reloadLabel } from '$lib/settingFields';

	export let settings: SettingOverride[] = [];
	export let canWrite = false;
	/** Called after a save or reset so the page can re-read values and status. */
	export let onSaved: () => void = () => {};

	let drafts: Record<string, string> = {};
	let busy: Record<string, boolean> = {};
	let errors: Record<string, string> = {};
	let saved: Record<string, boolean> = {};
	let probes: Record<string, SettingProbe | undefined> = {};

	// Seed each draft from the server value, and re-seed when the server answers
	// with something else (a save, a reset, a re-check) - but never while the
	// operator is mid-edit on that field.
	$: for (const s of settings) {
		if (drafts[s.key] === undefined) drafts[s.key] = String(s.value ?? '');
	}

	function draftFor(s: SettingOverride): string {
		return drafts[s.key] ?? String(s.value ?? '');
	}

	async function save(s: SettingOverride) {
		busy = { ...busy, [s.key]: true };
		errors = { ...errors, [s.key]: '' };
		saved = { ...saved, [s.key]: false };
		try {
			const raw = draftFor(s);
			const answer = await api.saveSettingOverride(s.key, s.type === 'int' ? Number(raw) : raw);
			// What the cluster said while agreeing - which node a template was found
			// on, or that a VLAN could not be verified. Kept, not swallowed: a save
			// that succeeded with a caveat has to show the caveat.
			const probe = (answer as { probe?: { ok: boolean; detail: string } | null }).probe;
			probes = {
				...probes,
				[s.key]: probe ? { key: s.key, ok: probe.ok, reachable: true, detail: probe.detail } : undefined,
			};
			saved = { ...saved, [s.key]: true };
			drafts = { ...drafts, [s.key]: undefined as unknown as string };
			onSaved();
		} catch (e) {
			// The server's own words: a 409 explains which variable overrides this
			// setting and that nothing was recorded, which no generic message can.
			errors = { ...errors, [s.key]: e instanceof ApiError ? e.detail : String(e) };
		} finally {
			busy = { ...busy, [s.key]: false };
		}
	}

	async function test(s: SettingOverride) {
		busy = { ...busy, [s.key]: true };
		errors = { ...errors, [s.key]: '' };
		saved = { ...saved, [s.key]: false };
		try {
			const raw = draftFor(s);
			probes = {
				...probes,
				[s.key]: await api.probeSettingOverride(s.key, s.type === 'int' ? Number(raw) : raw),
			};
		} catch (e) {
			errors = { ...errors, [s.key]: e instanceof ApiError ? e.detail : String(e) };
		} finally {
			busy = { ...busy, [s.key]: false };
		}
	}

	async function reset(s: SettingOverride) {
		busy = { ...busy, [s.key]: true };
		errors = { ...errors, [s.key]: '' };
		probes = { ...probes, [s.key]: undefined };
		try {
			await api.clearSettingOverride(s.key);
			drafts = { ...drafts, [s.key]: undefined as unknown as string };
			onSaved();
		} catch (e) {
			errors = { ...errors, [s.key]: e instanceof ApiError ? e.detail : String(e) };
		} finally {
			busy = { ...busy, [s.key]: false };
		}
	}
</script>

{#if settings.length}
	<div class="section-stack border-t border-line pt-3">
		{#each settings as s (s.key)}
			<div class="space-y-1" data-setting={s.key}>
				<div class="flex items-baseline gap-2 flex-wrap">
					<label class="field-label" for={`setting-${s.key}`}>{fieldLabel(s.key)}</label>
					<span class="text-xs text-muted">{reloadLabel(s)}</span>
				</div>
				{#if s.source === 'env'}
					<p class="text-xs font-mono text-ink">{s.value === '' ? '(empty)' : s.value}</p>
					<p class="text-xs text-muted">{envLockNote(s)}</p>
				{:else if canWrite}
					<div class="flex items-center gap-2 flex-wrap">
						<input
							id={`setting-${s.key}`}
							class="input w-64"
							type={s.type === 'int' ? 'number' : 'text'}
							value={draftFor(s)}
							on:input={(e) => (drafts = { ...drafts, [s.key]: e.currentTarget.value })}
						/>
						<button class="btn btn-sm" disabled={busy[s.key]} on:click={() => save(s)}>
							{busy[s.key] ? 'Saving…' : 'Save'}
						</button>
						{#if s.probeable}
							<button class="btn btn-ghost btn-sm" disabled={busy[s.key]} on:click={() => test(s)}>
								Test
							</button>
						{/if}
						{#if s.source === 'db'}
							<button class="btn btn-ghost btn-sm" disabled={busy[s.key]} on:click={() => reset(s)}>
								Reset to default
							</button>
						{/if}
						{#if saved[s.key]}
							<span class="text-xs text-ok">Saved</span>
						{/if}
					</div>
				{:else}
					<p class="text-xs font-mono text-ink">{s.value === '' ? '(empty)' : s.value}</p>
				{/if}
				<p class="prose-note prose-measure text-xs">{s.description}</p>
				{#if s.source === 'db'}
					<p class="text-xs text-muted">Saved here, in this instance's database.</p>
				{/if}
				{#if probes[s.key]}
					<!-- The cluster's own sentence, verbatim: "no bridge vmbr7 on node
					     pve1; node has: vmbr0, vmbr1" is the answer an operator can act
					     on, and no paraphrase of it is. -->
					<p
						class="text-xs {probes[s.key]?.ok ? 'text-ok' : 'text-danger'}"
						data-probe={s.key}
					>
						{probes[s.key]?.detail}
					</p>
				{/if}
				{#if errors[s.key]}
					<p class="text-xs text-danger" data-error={s.key}>{errors[s.key]}</p>
				{/if}
			</div>
		{/each}
	</div>
{/if}
