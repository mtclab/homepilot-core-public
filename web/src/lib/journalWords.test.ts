import { describe, it, expect } from 'vitest';
import { actorName, describeJournal, sentence } from './journalWords';
import type { AuditEntry } from './api';

/**
 * "What happened" in words (3.12): ONE formatter, a table of known actions,
 * and the raw action name for anything else - never a guess.
 *
 * Teeth: give an unknown action a made-up verb and the "as recorded" test
 * fails; drop the grouping and "withdrew 4" reads as four lines.
 */
let n = 0;
function e(over: Partial<AuditEntry>): AuditEntry {
	n += 1;
	return {
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
}
const ME = { token_label: 'owner', prefix: 'hp_owner000001' };

describe('journal words', () => {
	it('names the reader "You" and anyone else by their label', () => {
		expect(actorName('owner (hp_owner000001)', ME)).toBe('You');
		expect(actorName('claude (hp_asst0000001)', ME)).toBe('claude');
		expect(actorName('system', ME)).toBe('HomePilot');
		expect(actorName('admin-secret', ME)).toBe('admin-secret');
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

	it('says an unknown action as recorded, never a paraphrase', () => {
		const out = describeJournal([e({ action: 'tailnet_join', target_host: 'relay', user_id: 'x (hp_1)' })], ME);
		expect(sentence(out[0])).toBe('x: tailnet_join (relay)');
	});
});
