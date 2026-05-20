import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle, LifecycleError
from homepilot.artifacts.models import (
    ArtifactStatus,
    compute_body_hash,
)
from homepilot.artifacts.validator import validate_composite_spec


def _composite_body(artifact_id: str, step_id: str = "s1") -> str:
    return f"```yaml composite-spec\nsteps:\n  - id: {step_id}\n    artifact: {artifact_id}\n```\n"


def _make_spec(**overrides) -> dict:
    spec = {
        "id": "2025-01-01-test-artifact-abc123",
        "kind": "ansible-playbook",
        "intent": "Install nginx on web server",
        "body": "---\n- name: Install nginx\n  hosts: all\n  tasks: []",
        "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }
    spec.update(overrides)
    return spec


class TestPropose:
    async def test_propose_creates_artifact(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(_make_spec())
        assert aid == "2025-01-01-test-artifact-abc123"
        fm, _body = mock_store.read(aid)
        assert fm["status"] == "proposed"
        assert fm["kind"] == "ansible-playbook"
        assert fm["mutating"] is True

    async def test_propose_duplicate_raises(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        await lc.propose(_make_spec())
        with pytest.raises(LifecycleError, match="already exists"):
            await lc.propose(_make_spec())

    async def test_propose_invalid_id(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        with pytest.raises(LifecycleError, match="Invalid artifact ID"):
            await lc.propose(_make_spec(id="bad-id"))

    async def test_propose_kb_note_skips_approval(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(
            _make_spec(
                kind="kb-note",
                target=None,
                idempotence=None,
                body="Some note content",
            )
        )
        fm, _ = mock_store.read(aid)
        assert fm["status"] == "applied"


class TestApprove:
    async def test_approve_proposed(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        fm, _ = mock_store.read(aid)
        assert fm["status"] == "approved"
        assert fm["approved_by"]["user"] == "admin"

    async def test_approve_with_reason(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin", reason="LGTM")
        fm, _ = mock_store.read(aid)
        assert fm["approved_by"]["reason"] == "LGTM"

    async def test_approve_hash_mismatch(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(_make_spec())
        fm, body = mock_store.read(aid)
        body = body + "\nTAMPERED"
        mock_store._storage[aid] = (fm, body)
        with pytest.raises(LifecycleError, match="Hash mismatch"):
            await lc.approve(aid, user="admin")


class TestReject:
    async def test_reject_proposed(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(_make_spec())
        await lc.reject(aid, user="admin", reason="bad idea")
        fm, _ = mock_store.read(aid)
        assert fm["status"] == "rejected"
        assert fm["rejected_by"]["user"] == "admin"

    async def test_cannot_approve_rejected(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(_make_spec())
        await lc.reject(aid, user="admin")
        with pytest.raises(LifecycleError, match="Invalid transition"):
            await lc.approve(aid, user="admin")


class TestApply:
    async def test_apply_approved(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        await lc.mark_applied(aid, execution_log="done")
        fm, _ = mock_store.read(aid)
        assert fm["status"] == "applied"
        assert fm["applied_at"] is not None


class TestTransitionValidation:
    @pytest.mark.parametrize(
        "from_status,to_status,valid",
        [
            ("proposed", "approved", True),
            ("proposed", "rejected", True),
            ("proposed", "applied", False),
            ("approved", "applied", True),
            ("approved", "failed", True),
            ("approved", "revoked", True),
            ("applied", "superseded", True),
            ("applied", "revoked", True),
            ("rejected", "approved", False),
        ],
    )
    def test_transitions(self, mock_store, from_status, to_status, valid):
        lc = ArtifactLifecycle(store=mock_store)
        current = ArtifactStatus(from_status)
        target = ArtifactStatus(to_status)
        if valid:
            lc._validate_transition(current, target)
        else:
            with pytest.raises(LifecycleError):
                lc._validate_transition(current, target)


class TestHashVerification:
    def test_compute_body_hash_stable(self):
        body = "---\n- name: test\n  tasks: []\n"
        h1 = compute_body_hash(body)
        h2 = compute_body_hash(body)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_compute_body_hash_normalizes_trailing(self):
        b1 = "hello\n"
        b2 = "hello\n\n\n"
        assert compute_body_hash(b1) == compute_body_hash(b2)

    def test_compute_body_hash_different_content(self):
        assert compute_body_hash("aaa") != compute_body_hash("bbb")


class TestCascadeInvalidate:
    async def test_cascade_resets_approved_composite(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        child_id = await lc.propose(_make_spec(id="2025-01-01-child-ref-a1b2c3"))
        await lc.approve(child_id, user="admin")

        composite_spec = _make_spec(
            id="2025-01-01-comp-ref-d4e5f6",
            kind="composite",
            target={"kind": "cluster"},
            idempotence="replay-only",
            body=_composite_body(child_id),
        )
        comp_id = await lc.propose(composite_spec)
        await lc.approve(comp_id, user="admin")

        fm, body = mock_store.read(child_id)
        mock_store._storage[child_id] = (fm, body + "\n# extra line")
        await lc.edit(child_id)

        comp_fm, _ = mock_store.read(comp_id)
        assert comp_fm["status"] == "proposed"

    async def test_cascade_visited_set_prevents_revisit(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        child_id = await lc.propose(_make_spec(id="2025-01-01-child-ref-a1b2c3"))
        await lc.approve(child_id, user="admin")

        comp1_spec = _make_spec(
            id="2025-01-01-comp1-ref-d4e5f6",
            kind="composite",
            target={"kind": "cluster"},
            idempotence="replay-only",
            body=_composite_body(child_id),
        )
        comp1_id = await lc.propose(comp1_spec)
        await lc.approve(comp1_id, user="admin")

        fm, body = mock_store.read(child_id)
        mock_store._storage[child_id] = (fm, body + "\n# extra line")
        await lc.edit(child_id)
        fm1, _ = mock_store.read(comp1_id)
        assert fm1["status"] == "proposed"


class TestCompositeSpecValidation:
    def test_valid_composite_spec(self):
        body = (
            "```yaml composite-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    artifact: 2025-01-01-child-ref-a1b2c3\n"
            "  - id: step2\n"
            "    artifact: 2025-01-01-child-ref-d4e5f6\n"
            "```\n"
        )
        validate_composite_spec(body)

    def test_empty_body_for_composite_raises(self):
        with pytest.raises(LifecycleError, match="composite spec must have at least one step"):
            validate_composite_spec("")

    def test_composite_spec_no_steps_key_raises(self):
        body = "```yaml composite-spec\nother: true\n```\n"
        with pytest.raises(LifecycleError, match="composite spec must have at least one step"):
            validate_composite_spec(body)

    def test_composite_spec_empty_steps_raises(self):
        body = "```yaml composite-spec\nsteps: []\n```\n"
        with pytest.raises(LifecycleError, match="at least one step"):
            validate_composite_spec(body)

    def test_composite_spec_missing_step_id_raises(self):
        body = "```yaml composite-spec\nsteps:\n  - artifact: 2025-01-01-child-ref-a1b2c3\n```\n"
        with pytest.raises(LifecycleError, match="must have an 'id'"):
            validate_composite_spec(body)

    def test_composite_spec_missing_artifact_raises(self):
        body = "```yaml composite-spec\nsteps:\n  - id: step1\n```\n"
        with pytest.raises(LifecycleError, match="must have an 'artifact' reference"):
            validate_composite_spec(body)

    def test_composite_spec_duplicate_step_ids_raises(self):
        body = (
            "```yaml composite-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    artifact: 2025-01-01-a-a1b2c3\n"
            "  - id: step1\n"
            "    artifact: 2025-01-01-b-d4e5f6\n"
            "```\n"
        )
        with pytest.raises(LifecycleError, match="duplicate step id"):
            validate_composite_spec(body)

    async def test_propose_composite_validates_body(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store)
        with pytest.raises(LifecycleError, match="composite spec must have at least one step"):
            await lc.propose(
                _make_spec(
                    id="2025-01-01-comp-badbody-x1y2z3",
                    kind="composite",
                    target={"kind": "cluster"},
                    idempotence="replay-only",
                    body="just some text without spec block",
                )
            )
