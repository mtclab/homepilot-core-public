import { describe, it, expect } from 'vitest';
import { actorName, describeJournal, sentence, type WordedEntry } from './journalWords';
import type { AuditEntry } from './api';
import { journalWordsFor } from './wordingFixture';

/**
 * "What happened" in words (3.12; the words are the server's since 3.14): the
 * rows arrive with `words` (dashboard/wording.py, via the generated
 * wording.fixture.json here), and this only says who and groups.
 *
 * Teeth: print `e.action` instead of the server's words and every sentence
 * test fails; drop the grouping and "withdrew 4" reads as four lines.
 */
let n = 0;
function e(over: Partial<AuditEntry>): WordedEntry {
	n += 1;
	const row: AuditEntry = {
		id: n,
		timestamp: '2026-11-12T20:18:00Z',
		user_id: 'owner (hp_owner000001)',
		source: 'api',
		action: 'host_added',
		artifact_id: null,
		target_host: null,
		target_service: null,
		command: null,
		exit_code: null,
		snapshot_id: null,
		duration_ms: null,
		details_json: null,
		...over,
	};
	return { ...row, words: journalWordsFor(row) };
}
const ME = { token_label: 'owner', prefix: 'hp_owner000001' };

describe('journal words', () => {
	it('names the reader "You" and anyone else by their label', () => {
		expect(actorName('owner (hp_owner000001)', ME)).toBe('You');
		expect(actorName('claude (hp_asst0000001)', ME)).toBe('claude');
		expect(actorName('system', ME)).toBe('HomePilot');
		expect(actorName('admin-secret', ME)).toBe('admin-secret');
	});

	it('names an internal-id actor by the server\'s words, never the id (3.14 walk)', () => {
		const id = '5f0c2a1e-7b3d-4c9e-9a41-0d2e6b8f3c17';
		expect(actorName(id, ME, 'claude')).toBe('claude');
		// An older server that sent no actor words: still not the id.
		expect(actorName(id, ME)).toBe('A token that no longer exists');
		const row = e({ action: 'host_added', user_id: id, target_host: 'nas' });
		const said = describeJournal([{ ...row, words: { ...row.words!, actor: 'claude' } }], ME);
		expect(sentence(said[0])).toBe('claude added the host nas');
		expect(sentence(describeJournal([row], ME)[0])).not.toContain(id);
	});

	it('says known actions in words, with what they acted on', () => {
		const out = describeJournal(
			[
				e({ action: 'install_agent', target_host: 'infra-docker', details_json: JSON.stringify({ upgrade: true, agent_version: 'v3.11.0' }) }),
				e({ action: 'host_forgotten', user_id: 'system', target_host: 'probe-1', details_json: JSON.stringify({ reason: 'destroyed by an applied change' }) }),
				e({ action: 'approve', user_id: 'claude (hp_asst0000001)', artifact_id: '2026-11-08-retire-104' }),
			],
			ME,
			'/ui',
		);
		expect(out.map(sentence)).toEqual([
			'You upgraded the agent on infra-docker to v3.11.0',
			'HomePilot forgot probe-1, which an applied change destroyed',
			'claude approved 2026-11-08-retire-104',
		]);
		expect(out[2].href).toBe('/ui/changes/2026-11-08-retire-104');
		expect(out[0].parts.find((p) => p.strong)?.text).toBe('infra-docker');
	});

	it('makes one sentence of the same actor doing the same thing in a row', () => {
		const rows = [1, 2, 3, 4].map((i) => e({ action: 'withdraw', user_id: 'claude (hp_asst0000001)', artifact_id: `a-${i}` }));
		const out = describeJournal([...rows, e({ action: 'withdraw', artifact_id: 'mine' })], ME);
		expect(out.map(sentence)).toEqual(['claude withdrew 4 approved changes', 'You withdrew mine']);
		expect(out[0].count).toBe(4);
	});

	it("says every action in the server's words, never its code", () => {
		const out = describeJournal(
			[
				e({ action: 'tailnet_join', target_host: 'relay', user_id: 'x (hp_1)' }),
				e({ action: 'apply_refused', artifact_id: '2026-11-18-shell-web01-abc123' }),
				// A code no current version writes is still words.
				e({ action: 'composite-rollback' }),
			],
			ME,
		);
		expect(out.map(sentence)).toEqual([
			'x joined the tailnet: relay',
			'You tried to apply 2026-11-18-shell-web01-abc123 - stopped: the machine had changed',
			'You recorded: composite rollback',
		]);
		for (const h of out) expect(sentence(h)).not.toMatch(/[a-z]+_[a-z]+/);
	});
});
