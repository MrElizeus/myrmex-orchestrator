#!/usr/bin/env python3
"""Standalone tests for the WU-P1-003 source-observation and import-operation contracts.

Covers: meta-validation of both 2020-12 schemas; positive fixtures for all five
source-observation outcomes and all four import-operation lifecycle statuses;
negative schema fixtures (unknown fields, missing required fields, malformed
IDs/digests, unknown enums, authority flag violations, status/outcome
conditionals); semantic digest derivations with corruption rejection;
state-first ordering (intent persisted before the reader runs); changed/
unchanged outcome mapping; TimeoutError -> unavailable; invalid/ambiguous
preservation; idempotent replay; negative replay conflict; identity conflicts;
intermediate crash recovery; campaign.json immutability; the reader adapter
boundary; raw-content and secret rejection before any artifact; lifecycle chain
and observation/receipt continuity; artifact kind mapping and projection
descriptor hygiene.

The fixtures are deterministic: every digest is derived from fixed constants
via the module's own canonical JSON helpers, and all campaign state lives in
fresh temporary directories.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
BIN_CAMPAIGN = ROOT / "bin/myrmex-campaign"
CONTRACTS = ROOT / "contracts"

sys.path.insert(0, str(SCRIPTS_DIR))
import myrmex_backlog_import as bkg  # noqa: E402
import myrmex_campaign_intelligence as intel  # noqa: E402

SOURCE_OBSERVATION_SCHEMA_PATH = CONTRACTS / "source-observation-v1.schema.json"
IMPORT_OPERATION_SCHEMA_PATH = CONTRACTS / "import-operation-v1.schema.json"
SOURCE_OBSERVATION_ID = "urn:myrmex:schema:source-observation:v1"
IMPORT_OPERATION_ID = "urn:myrmex:schema:import-operation:v1"

CAMPAIGN_ID = "camp-p1-campaign-intelligence"
SOURCE_IDENTITY = {"kind": "jira", "canonical_id": "PROJ-1"}
ADAPTER = "jira"
RUNTIME_REQUEST = {"issue": "INC-42", "fields": ["title", "status", "priority"]}
RUNTIME_IDEMPOTENCY = "p1-003-runtime-0001"
CREATED_AT = "2026-08-06T22:30:00+00:00"

HEX64_A = "ab" * 32
HEX64_B = "cd" * 32
HEX64_C = "ef" * 32
HEX64_D = "12" * 32

# Sentinel distinguishing "not provided" from an explicit None override.
_UNSET = object()

TEST_COUNT = 0


def ok(label: str) -> None:
    global TEST_COUNT
    TEST_COUNT += 1
    print(f"  ok {label}")


def load_schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Campaign fixtures (fresh temp XDG_STATE_HOME per test)
# ---------------------------------------------------------------------------

def run_campaign(args: list[str], state_home: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, XDG_STATE_HOME=state_home, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, str(BIN_CAMPAIGN), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def campaign_dir(state_home: str, cid: str = CAMPAIGN_ID) -> Path:
    return Path(state_home) / "myrmex" / "campaigns" / cid


def init_campaign(state_home: str, cid: str = CAMPAIGN_ID) -> Path:
    repo = Path(state_home) / "repo"
    repo.mkdir(exist_ok=True)
    proc = run_campaign(
        ["init", "--id", cid, "--title", "P1-003", "--objective", "Source import", "--repo-root", str(repo)],
        state_home,
    )
    assert proc.returncode == 0, f"campaign init failed: {proc.stderr}"
    cdir = campaign_dir(state_home, cid)
    data = json.loads((cdir / "campaign.json").read_text(encoding="utf-8"))
    return cdir, data


def artifact_file_count(cdir: Path) -> int:
    arts = cdir / "intelligence" / "artifacts"
    if not arts.is_dir():
        return 0
    return len(list(arts.glob("*.json")))


def tamper_artifact(cdir: Path, campaign_id: str, artifact_id: str, mutate_payload) -> None:
    """Tamper a stored P1-003 payload directly on disk, bypassing the normal
    immutable put_artifact API, and recompute the outer P1-002 envelope digests
    so the envelope remains internally valid.

    The mutate_payload callback is responsible for recomputing any inner P1-003
    digest-derived identities (record_digest/record_id or
    observation_digest/observation_id) so the P1-003 contract is internally
    consistent and can only be rejected by the runtime trust-boundary checks.
    """
    path = bkg._artifact_path(cdir, artifact_id)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate_payload(envelope["payload"])
    envelope["payload_digest"] = intel.compute_payload_digest(envelope["payload"])
    envelope["artifact_digest"] = intel.compute_artifact_digest(
        envelope["campaign_id"], envelope["kind"], envelope["artifact_id"], envelope["payload"]
    )
    path.write_bytes(intel.canonical_json_bytes(envelope) + b"\n")


# ---------------------------------------------------------------------------
# Contract fixture builders (deterministic)
# ---------------------------------------------------------------------------

def make_source_observation(
    outcome: str = "changed",
    previous: str | None = None,
    content: str | None | object = _UNSET,
    observed_version: str | None | object = _UNSET,
    reason: str | None | object = _UNSET,
    **overrides: object,
) -> dict:
    """Build a source-observation payload with derived ids/digests recomputed.

    Derived-field overrides (observation_id/observation_digest) are applied
    last so fixtures can deliberately corrupt a derivation.
    """
    if outcome in ("changed", "unchanged"):
        if content is _UNSET:
            content = HEX64_A
        if observed_version is _UNSET:
            observed_version = "v7"
        reason = None
    elif outcome in ("unavailable", "invalid"):
        if content is _UNSET:
            content = None
        if observed_version is _UNSET:
            observed_version = None
        if reason is _UNSET:
            reason = "source_unavailable"
    elif outcome == "ambiguous":
        if content is _UNSET:
            content = None
        if reason is _UNSET:
            reason = "multiple_matches"
    else:
        raise ValueError(f"unknown outcome {outcome!r}")

    obs = {
        "schema": "myrmex.source-observation/v1",
        "campaign_id": CAMPAIGN_ID,
        "operation_id": "importop-" + "01" * 12,
        "source_identity": SOURCE_IDENTITY,
        "adapter": ADAPTER,
        "request_digest": HEX64_B,
        "previous_content_digest": previous,
        "observed_version": observed_version,
        "content_digest": content,
        "outcome": outcome,
        "reason_code": reason,
        "observed_at": CREATED_AT,
        "authority": dict(bkg.OBSERVATION_AUTHORITY),
    }
    derived = {k: v for k, v in overrides.items() if k in ("observation_id", "observation_digest")}
    obs.update({k: v for k, v in overrides.items() if k not in derived})
    obs["observation_digest"] = bkg.compute_observation_digest(obs)
    obs["observation_id"] = bkg.derive_observation_id(obs["observation_digest"])
    obs.update(derived)
    return obs


def make_observation_ref(outcome: str = "changed") -> dict:
    obs = make_source_observation(outcome)
    return {
        "observation_id": obs["observation_id"],
        "observation_digest": obs["observation_digest"],
        "outcome": obs["outcome"],
    }


def make_receipt(outcome: str = "changed", operation_id: str | None = None) -> dict:
    obs = make_source_observation(outcome)
    return bkg._build_receipt(
        operation_id=operation_id or "importop-" + "01" * 12,
        observation_id=obs["observation_id"],
        observation_digest=obs["observation_digest"],
        request_digest=HEX64_B,
        outcome=outcome,
    )


def make_import_record(
    status: str = "intent",
    observation: dict | None = None,
    receipt: dict | None = None,
    previous_record_id: str | None = None,
    request: dict | None = None,
    **overrides: object,
) -> dict:
    """Build an import-operation record with derived record ids/digests recomputed.

    Derived-field overrides (record_id/record_digest) are applied last so
    fixtures can deliberately corrupt a derivation.
    """
    req = request if request is not None else {"issue": "INC-1", "fields": ["title"]}
    record = {
        "schema": "myrmex.import-operation/v1",
        "operation_id": bkg.derive_operation_id(CAMPAIGN_ID, "import-key-1"),
        "idempotency_key": "import-key-1",
        "campaign_id": CAMPAIGN_ID,
        "source_identity": SOURCE_IDENTITY,
        "adapter": ADAPTER,
        "request": req,
        "request_digest": bkg.compute_request_digest(req),
        "previous_content_digest": None,
        "status": status,
        "effect_stage": "none" if status == "intent" else "response_observed",
        "observation": observation,
        "receipt": receipt,
        "previous_record_id": previous_record_id,
        "authority": dict(bkg.IMPORT_AUTHORITY),
        "created_at": CREATED_AT,
    }
    derived = {k: v for k, v in overrides.items() if k in ("record_id", "record_digest")}
    record.update({k: v for k, v in overrides.items() if k not in derived})
    record["record_digest"] = bkg.compute_record_digest(record)
    record["record_id"] = bkg.derive_record_id(record["record_digest"])
    record.update(derived)
    return record


def make_chain_records() -> dict:
    """Deterministic full lifecycle chain used by positive fixtures and tests."""
    obs = make_source_observation("changed")
    intent = make_import_record("intent")
    effect = make_import_record(
        "effect-observed",
        observation=make_observation_ref("changed"),
        previous_record_id=intent["record_id"],
    )
    receipt = make_receipt("changed", operation_id=intent["operation_id"])
    receipt_recorded = make_import_record(
        "receipt-recorded",
        observation=effect["observation"],
        receipt=receipt,
        previous_record_id=effect["record_id"],
    )
    confirmed = make_import_record(
        "confirmed",
        observation=effect["observation"],
        receipt=receipt,
        previous_record_id=receipt_recorded["record_id"],
    )
    return {
        "observation": obs,
        "intent": intent,
        "effect": effect,
        "receipt": receipt,
        "receipt_recorded": receipt_recorded,
        "confirmed": confirmed,
    }


# ---------------------------------------------------------------------------
# Runtime reader factory
# ---------------------------------------------------------------------------

def make_reader(
    result_factory,
    *,
    campaign_dir: Path,
    campaign_id: str,
    idempotency_key: str,
    state: dict,
):
    """A fake reader that enforces the adapter boundary and state-first ordering.

    It asserts (before returning) that the import-operation/<operation_id>/intent
    artifact is already durable in the P1-002 sidecar and validates, setting
    state['reader_called_after_durable_intent'] = True.
    """
    calls: list[dict] = []

    def reader(ctx: dict) -> dict:
        calls.append(dict(ctx))
        # Adapter boundary: EXACTLY the allowed keys and no capability leaks.
        assert set(ctx) == {"source_identity", "adapter", "request", "request_digest"}, (
            f"reader context keys must be exactly {{source_identity, adapter, request, request_digest}}, got {set(ctx)}"
        )
        for forbidden in (
            "campaign_dir", "campaign_id", "campaign_revision", "writer",
            "wu_add", "wu_transition", "activate_plan", "commit", "push",
            "credentials", "token", "secret", "repository_root",
        ):
            assert forbidden not in ctx, f"reader context must not expose {forbidden}"
        # State-first: the intent must be durably persisted before the reader runs.
        operation_id = bkg.derive_operation_id(campaign_id, idempotency_key)
        intent_payload = bkg._try_get_payload(
            campaign_dir, campaign_id, f"import-operation/{operation_id}/intent"
        )
        assert intent_payload is not None, "intent must be durably persisted before reader runs"
        assert intent_payload["status"] == "intent"
        assert intent_payload["operation_id"] == operation_id
        assert intent_payload["campaign_id"] == campaign_id
        assert intent_payload["request_digest"] == ctx["request_digest"]
        assert bkg.validate_import_record_semantics(intent_payload) == []
        state["reader_called_after_durable_intent"] = True

        result = result_factory()
        if isinstance(result, BaseException):
            raise result
        return result

    return reader, calls


def run_operation(cdir: Path, data: dict, idempotency_key: str = RUNTIME_IDEMPOTENCY,
                  request: dict | None = None, previous: str | None = None,
                  source_identity: dict | None = None, adapter: str | None = None,
                  reader=None, reader_state: dict | None = None):
    reader_state = reader_state if reader_state is not None else {}
    if reader is None:
        reader, _ = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=idempotency_key,
            state=reader_state,
        )
    return bkg.execute_import_operation(
        campaign_dir=cdir,
        campaign_id=data["id"],
        campaign_revision=data["revision"],
        idempotency_key=idempotency_key,
        source_identity=source_identity if source_identity is not None else SOURCE_IDENTITY,
        adapter=adapter if adapter is not None else ADAPTER,
        request=request if request is not None else RUNTIME_REQUEST,
        previous_content_digest=previous,
        reader=reader,
    )


def run_operation_until(cdir: Path, data: dict, fail_at_artifact_id: str,
                        idempotency_key: str = RUNTIME_IDEMPOTENCY) -> None:
    """Run one operation with a simulated persistence failure at the given
    artifact id, leaving the sidecar in the durable state just before that
    artifact would have been written."""
    original_put = intel.put_artifact

    def failing_put(campaign_dir, campaign_id, campaign_revision, kind, artifact_id, payload):
        if artifact_id == fail_at_artifact_id:
            raise RuntimeError("simulated lifecycle persist failure")
        return original_put(campaign_dir, campaign_id, campaign_revision, kind, artifact_id, payload)

    intel.put_artifact = failing_put
    try:
        try:
            run_operation(cdir, data, idempotency_key=idempotency_key)
            raise AssertionError(f"operation must fail when persisting {fail_at_artifact_id}")
        except RuntimeError as exc:
            assert "simulated lifecycle persist failure" in str(exc)
    finally:
        intel.put_artifact = original_put


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_schema_meta_and_identity() -> None:
    import jsonschema

    source_schema = load_schema(SOURCE_OBSERVATION_SCHEMA_PATH)
    import_schema = load_schema(IMPORT_OPERATION_SCHEMA_PATH)

    assert source_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert source_schema["$id"] == SOURCE_OBSERVATION_ID
    assert source_schema["properties"]["schema"]["const"] == "myrmex.source-observation/v1"
    assert source_schema["additionalProperties"] is False
    assert import_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert import_schema["$id"] == IMPORT_OPERATION_ID
    assert import_schema["properties"]["schema"]["const"] == "myrmex.import-operation/v1"
    assert import_schema["additionalProperties"] is False

    jsonschema.Draft202012Validator.check_schema(source_schema)
    jsonschema.Draft202012Validator.check_schema(import_schema)

    # Required-field completeness.
    for field in (
        "schema", "observation_id", "observation_digest", "campaign_id",
        "operation_id", "source_identity", "adapter", "request_digest",
        "previous_content_digest", "observed_version", "content_digest",
        "outcome", "reason_code", "observed_at", "authority",
    ):
        assert field in source_schema["required"], field
    for field in (
        "schema", "record_id", "record_digest", "operation_id", "idempotency_key",
        "campaign_id", "source_identity", "adapter", "request", "request_digest",
        "previous_content_digest", "status", "effect_stage", "observation",
        "receipt", "previous_record_id", "authority", "created_at",
    ):
        assert field in import_schema["required"], field
    ok("both schemas meta-validate and carry exact identity/const invariants")


def test_positive_schema_fixtures() -> None:
    import jsonschema

    source_validator = jsonschema.Draft202012Validator(load_schema(SOURCE_OBSERVATION_SCHEMA_PATH))
    import_validator = jsonschema.Draft202012Validator(load_schema(IMPORT_OPERATION_SCHEMA_PATH))

    source_positives = {
        "changed": make_source_observation("changed", previous=None, content=HEX64_A),
        "unchanged": make_source_observation("unchanged", previous=HEX64_A, content=HEX64_A),
        "unavailable": make_source_observation("unavailable", reason="source_unavailable"),
        "invalid": make_source_observation("invalid", reason="bad_request"),
        "ambiguous-null-version": make_source_observation("ambiguous", observed_version=None, reason="multiple_matches"),
        "ambiguous-string-version": make_source_observation("ambiguous", observed_version="branch-x", reason="multiple_matches"),
    }
    for name, instance in source_positives.items():
        errs = list(source_validator.iter_errors(instance))
        assert not errs, f"source-observation positive {name} failed: {[e.message for e in errs][:3]}"

    chain = make_chain_records()
    import_positives = {
        "intent": chain["intent"],
        "effect-observed": chain["effect"],
        "receipt-recorded": chain["receipt_recorded"],
        "confirmed": chain["confirmed"],
    }
    for name, instance in import_positives.items():
        errs = list(import_validator.iter_errors(instance))
        assert not errs, f"import-operation positive {name} failed: {[e.message for e in errs][:3]}"
    ok(f"positive fixtures pass: {len(source_positives)} source-observation, {len(import_positives)} import-operation")


def _source_negative_fixtures() -> list[tuple[str, dict]]:
    return [
        ("unknown-top-level-field", make_source_observation("changed", extra_field=1)),
        ("missing-required", make_source_observation("changed", _drop="adapter")),
        ("malformed-campaign-id", make_source_observation("changed", campaign_id="camp-BAD!")),
        ("malformed-operation-id", make_source_observation("changed", operation_id="importop-xyz")),
        ("malformed-sha", make_source_observation("changed", observation_digest="not-hex")),
        ("malformed-observation-id", make_source_observation("changed", observation_id="srcobs_xyz")),
        ("unknown-outcome", {**make_source_observation("unavailable"), "outcome": "weird"}),
        ("authority-create-work-units", make_source_observation("changed", authority={**dict(bkg.OBSERVATION_AUTHORITY), "create_work_units": True})),
        ("authority-repository-write", make_source_observation("changed", authority={**dict(bkg.OBSERVATION_AUTHORITY), "repository_write": True})),
        ("authority-activate-plan", make_source_observation("changed", authority={**dict(bkg.OBSERVATION_AUTHORITY), "activate_plan": True})),
        ("authority-push", make_source_observation("changed", authority={**dict(bkg.OBSERVATION_AUTHORITY), "push": True})),
        ("invalid-carrying-content", make_source_observation("invalid", content=HEX64_A)),
        ("unavailable-carrying-content", make_source_observation("unavailable", content=HEX64_A)),
        ("changed-null-content", make_source_observation("changed", content=None)),
        ("unchanged-null-content", make_source_observation("unchanged", previous=HEX64_A, content=None)),
        ("changed-null-observed-version", make_source_observation("changed", observed_version=None)),
        ("unavailable-with-reason-null", make_source_observation("unavailable", reason=None)),
        ("malformed-source-kind", make_source_observation("changed", source_identity={"kind": "9bad", "canonical_id": "PROJ-1"})),
        ("empty-canonical-id", make_source_observation("changed", source_identity={"kind": "jira", "canonical_id": ""})),
        ("extra-source-identity-field", make_source_observation("changed", source_identity={**SOURCE_IDENTITY, "extra": True})),
    ]


def _import_negative_fixtures() -> list[tuple[str, dict]]:
    chain = make_chain_records()
    receipt = chain["receipt"]
    obs_ref = chain["effect"]["observation"]
    rec_id = "importrec_" + HEX64_C
    return [
        ("unknown-top-level-field", make_import_record("intent", extra_field=1)),
        ("missing-required", make_import_record("intent", _drop="request_digest")),
        ("malformed-record-id", make_import_record("intent", record_id="importrec_xyz")),
        ("malformed-record-digest", make_import_record("intent", record_digest="zz")),
        ("malformed-operation-id", make_import_record("intent", operation_id="importop-xyz")),
        ("malformed-observation-id", make_import_record("effect-observed", observation=dict(obs_ref, observation_id="srcobs_xyz"), previous_record_id=rec_id)),
        ("malformed-receipt-id", make_import_record("receipt-recorded", observation=obs_ref, receipt=dict(receipt, receipt_id="imprcpt_xyz"), previous_record_id=rec_id)),
        ("unknown-status", make_import_record("weird")),
        ("unknown-observation-outcome", make_import_record("effect-observed", observation=dict(obs_ref, outcome="weird"), previous_record_id=rec_id)),
        ("authority-create-work-units", make_import_record("intent", authority={**dict(bkg.IMPORT_AUTHORITY), "create_work_units": True})),
        ("authority-repository-write", make_import_record("intent", authority={**dict(bkg.IMPORT_AUTHORITY), "repository_write": True})),
        ("authority-activate-plan", make_import_record("intent", authority={**dict(bkg.IMPORT_AUTHORITY), "activate_plan": True})),
        ("authority-push", make_import_record("intent", authority={**dict(bkg.IMPORT_AUTHORITY), "push": True})),
        ("intent-carrying-observation", make_import_record("intent", observation=obs_ref)),
        ("intent-carrying-receipt", make_import_record("intent", receipt=receipt)),
        ("intent-with-previous-record", make_import_record("intent", previous_record_id=rec_id)),
        ("effect-observed-without-observation", make_import_record("effect-observed", observation=None, previous_record_id=rec_id)),
        ("effect-observed-with-receipt", make_import_record("effect-observed", observation=obs_ref, receipt=receipt, previous_record_id=rec_id)),
        ("receipt-recorded-without-receipt", make_import_record("receipt-recorded", observation=obs_ref, receipt=None, previous_record_id=rec_id)),
        ("confirmed-without-receipt", make_import_record("confirmed", observation=obs_ref, receipt=None, previous_record_id=rec_id)),
        ("intent-non-none-effect-stage", make_import_record("intent", effect_stage="response_observed")),
        ("request-not-object", make_import_record("intent", request="INC-1")),
    ]


def test_negative_schema_fixtures() -> None:
    import jsonschema

    source_validator = jsonschema.Draft202012Validator(load_schema(SOURCE_OBSERVATION_SCHEMA_PATH))
    import_validator = jsonschema.Draft202012Validator(load_schema(IMPORT_OPERATION_SCHEMA_PATH))

    for name, instance in _source_negative_fixtures():
        if "_drop" in instance:
            drop_key = instance.pop("_drop")
            instance = {k: v for k, v in instance.items() if k != drop_key}
        errs = list(source_validator.iter_errors(instance))
        assert errs, f"source-observation negative {name} unexpectedly validated"

    for name, instance in _import_negative_fixtures():
        if "_drop" in instance:
            drop_key = instance.pop("_drop")
            instance = {k: v for k, v in instance.items() if k != drop_key}
        errs = list(import_validator.iter_errors(instance))
        assert errs, f"import-operation negative {name} unexpectedly validated"
    ok("negative schema fixtures rejected: "
       f"{len(_source_negative_fixtures())} source-observation, {len(_import_negative_fixtures())} import-operation")


def test_semantic_digest_derivations() -> None:
    import jsonschema

    source_validator = jsonschema.Draft202012Validator(load_schema(SOURCE_OBSERVATION_SCHEMA_PATH))
    import_validator = jsonschema.Draft202012Validator(load_schema(IMPORT_OPERATION_SCHEMA_PATH))

    # operation_id derivation: does not include request content.
    op_a = bkg.derive_operation_id(CAMPAIGN_ID, "key-a")
    op_b = bkg.derive_operation_id(CAMPAIGN_ID, "key-b")
    assert op_a.startswith("importop-") and len(op_a) == 9 + 24
    assert op_a != op_b
    assert bkg.derive_operation_id(CAMPAIGN_ID, "key-a") == op_a
    # Same idempotency key with different request content yields the same id.
    req1 = {"issue": "INC-1"}
    req2 = {"issue": "INC-1", "fields": ["title"]}
    assert bkg.derive_operation_id(CAMPAIGN_ID, "same-key") == bkg.derive_operation_id(CAMPAIGN_ID, "same-key")
    assert bkg.compute_request_digest(req1) != bkg.compute_request_digest(req2)

    # Record-level derivations; corrupting each derived field is rejected.
    record = make_import_record("intent")
    assert record["record_id"] == "importrec_" + record["record_digest"]
    assert record["record_digest"] == bkg.compute_record_digest(record)
    assert record["operation_id"] == bkg.derive_operation_id(CAMPAIGN_ID, "import-key-1")
    assert record["request_digest"] == bkg.compute_request_digest(record["request"])
    for corrupt in (
        {"record_id": "importrec_" + HEX64_C},
        {"record_digest": HEX64_C},
        {"operation_id": "importop-" + "22" * 12},
        {"request_digest": HEX64_C},
    ):
        bad = make_import_record("intent", **corrupt)
        assert list(import_validator.iter_errors(bad)) == [], "corrupt digest must remain schema-valid"
        assert bkg.validate_import_record_semantics(bad), "corrupt digest must be semantically rejected"

    # Observation derivations.
    obs = make_source_observation("changed")
    assert obs["observation_id"] == "srcobs_" + obs["observation_digest"]
    assert obs["observation_digest"] == bkg.compute_observation_digest(obs)
    for corrupt in (
        {"observation_id": "srcobs_" + HEX64_C},
        {"observation_digest": HEX64_C},
    ):
        bad = make_source_observation("changed", **corrupt)
        assert list(source_validator.iter_errors(bad)) == [], "corrupt observation digest must remain schema-valid"
        assert bkg.validate_source_observation_semantics(bad), "corrupt observation digest must be semantically rejected"

    # Receipt derivations.
    receipt = make_receipt("changed", operation_id="importop-" + "01" * 12)
    assert receipt["receipt_id"] == "imprcpt_" + receipt["receipt_digest"]
    assert receipt["receipt_digest"] == bkg.compute_receipt_digest(receipt)
    for corrupt in (
        {"receipt_id": "imprcpt_" + HEX64_C},
        {"receipt_digest": HEX64_C},
    ):
        bad = dict(receipt, **corrupt)
        assert bkg.validate_receipt_semantics(bad), "corrupt receipt digest must be semantically rejected"
    ok("semantic digest derivations verified; corrupting operation_id/request_digest/observation_id/receipt_id/record_id each rejects")


# ---------------------------------------------------------------------------
# Runtime behavior tests
# ---------------------------------------------------------------------------

def test_state_first_ordering() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-statefirst-") as td:
        cdir, data = init_campaign(td)
        state: dict = {}
        reader, calls = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state=state,
        )
        result = run_operation(cdir, data, reader=reader, reader_state=state)
        assert result["ok"] is True and result["status"] == "confirmed"
        assert len(calls) == 1
        assert state.get("reader_called_after_durable_intent") is True
        ok("state-first ordering: intent durably persisted and validated before reader returns; reader called once")


def test_changed_unchanged_mapping() -> None:
    cases = [
        ("A-prev-null", None, HEX64_D, "changed"),
        ("B-prev-same", HEX64_A, HEX64_A, "unchanged"),
        ("C-prev-different", HEX64_A, HEX64_D, "changed"),
    ]
    for label, previous, content, expected in cases:
        with tempfile.TemporaryDirectory(prefix="p1c-mapping-") as td:
            cdir, data = init_campaign(td, cid="camp-p1-campaign-intelligence")
            reader, calls = make_reader(
                lambda content=content: {"status": "observed", "observed_version": "v9", "content_digest": content},
                campaign_dir=cdir, campaign_id=data["id"], idempotency_key=f"mapping-{label}", state={},
            )
            result = run_operation(cdir, data, idempotency_key=f"mapping-{label}", previous=previous, reader=reader)
            assert result["outcome"] == expected, f"{label}: expected {expected}, got {result['outcome']}"
            assert result["status"] == "confirmed"
            assert len(calls) == 1
    ok("changed/unchanged mapping: A previous null->changed, B previous D1 current D1->unchanged, C previous D1 current D2->changed")


def test_timeout_maps_to_unavailable() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-timeout-") as td:
        cdir, data = init_campaign(td)
        reader, calls = make_reader(
            lambda: TimeoutError("read timed out"),
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
        )
        result = run_operation(cdir, data, reader=reader)
        assert result["status"] == "confirmed"
        assert result["outcome"] == "unavailable"
        assert len(calls) == 1
        op_id = result["operation_id"]
        obs = bkg._try_get_payload(cdir, data["id"], f"source-observation/{op_id}")
        assert obs["outcome"] == "unavailable"
        assert obs["reason_code"] == "timeout"
        assert obs["observed_version"] is None and obs["content_digest"] is None
        assert result["receipt_id"].startswith("imprcpt_")
        ok("TimeoutError maps to unavailable/reason_code=timeout and still proceeds through receipt+confirmed")


def test_invalid_and_ambiguous_preserved() -> None:
    for status, reason in (("invalid", "bad_request"), ("ambiguous", "multiple_matches")):
        with tempfile.TemporaryDirectory(prefix="p1c-other-") as td:
            cdir, data = init_campaign(td)
            reader, calls = make_reader(
                lambda status=status, reason=reason: (
                    {"status": status, "reason_code": reason, "observed_version": None}
                    if status == "ambiguous"
                    else {"status": status, "reason_code": reason}
                ),
                campaign_dir=cdir, campaign_id=data["id"], idempotency_key=f"preserve-{status}", state={},
            )
            result = run_operation(cdir, data, idempotency_key=f"preserve-{status}", reader=reader)
            assert result["status"] == "confirmed"
            assert result["outcome"] == status
            assert len(calls) == 1
            op_id = result["operation_id"]
            obs = bkg._try_get_payload(cdir, data["id"], f"source-observation/{op_id}")
            assert obs["outcome"] == status
            assert obs["reason_code"] == reason
            assert obs["content_digest"] is None
    ok("invalid and ambiguous outcomes preserved through observation+receipt+confirmed")


def test_idempotent_replay() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-replay-") as td:
        cdir, data = init_campaign(td)
        state: dict = {}
        reader, calls = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state=state,
        )
        first = run_operation(cdir, data, reader=reader, reader_state=state)
        reader_count_before = len(calls)
        files_before = sorted(p.name for p in (cdir / "intelligence" / "artifacts").glob("*.json"))

        reader2, calls2 = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
        )
        second = run_operation(cdir, data, reader=reader2)
        reader_count_after = len(calls2)
        files_after = sorted(p.name for p in (cdir / "intelligence" / "artifacts").glob("*.json"))

        assert reader_count_before == 1
        assert reader_count_after == 0, "replay must not invoke the reader again"
        assert second["operation_id"] == first["operation_id"]
        assert second["observation_id"] == first["observation_id"]
        assert second["receipt_id"] == first["receipt_id"]
        assert second["confirmed_record_id"] == first["confirmed_record_id"]
        assert files_after == files_before, "replay must not create duplicate artifacts"
        assert artifact_file_count(cdir) == 5
        ok("idempotent replay: reader count unchanged (1 -> 1 with 0 new invocations), identical ids, no duplicate artifacts")


def test_negative_replay_conflict() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-negreplay-") as td:
        cdir, data = init_campaign(td)
        run_operation(cdir, data)
        files_before = sorted(p.name for p in (cdir / "intelligence" / "artifacts").glob("*.json"))
        bytes_before = {
            name: (cdir / "intelligence" / "artifacts" / name).read_bytes() for name in files_before
        }

        changed_request = {"issue": "INC-43", "fields": ["title", "different"]}
        reader, calls = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
        )
        try:
            run_operation(cdir, data, request=changed_request, reader=reader)
            raise AssertionError("changed request under the same idempotency key must conflict")
        except bkg.ImportOperationConflict:
            pass
        assert len(calls) == 0, "conflicting replay must not invoke the reader"
        files_after = sorted(p.name for p in (cdir / "intelligence" / "artifacts").glob("*.json"))
        assert files_after == files_before, "conflicting replay must not create new artifacts"
        for name in files_before:
            assert (cdir / "intelligence" / "artifacts" / name).read_bytes() == bytes_before[name]
        ok("negative replay: different request under same campaign+idempotency_key -> ImportOperationConflict, reader not invoked, artifacts unchanged")


def test_identity_conflicts() -> None:
    cases = [
        ("source_identity", lambda si=None: {"kind": "github", "canonical_id": "other-org/other"}),
        ("adapter", lambda ad=None: "github"),
        ("previous_content_digest", lambda pc=None: HEX64_C),
    ]
    for field, factory in cases:
        with tempfile.TemporaryDirectory(prefix="p1c-idconflict-") as td:
            cdir, data = init_campaign(td)
            run_operation(cdir, data)
            before = artifact_file_count(cdir)
            kwargs = {}
            if field == "source_identity":
                kwargs["source_identity"] = factory()
            elif field == "adapter":
                kwargs["adapter"] = factory()
            else:
                kwargs["previous"] = factory()
            reader, calls = make_reader(
                lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
                campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
            )
            try:
                run_operation(cdir, data, reader=reader, **kwargs)
                raise AssertionError(f"changed {field} under the same identity must conflict")
            except bkg.ImportOperationConflict:
                pass
            assert len(calls) == 0, f"changed {field} must conflict before the reader"
            assert artifact_file_count(cdir) == before, "conflict must not add artifacts"
    ok("identity conflicts: changed source_identity/adapter/previous_content_digest -> conflict before reader")


def test_intermediate_recovery() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-recovery-") as td:
        cdir, data = init_campaign(td)
        op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
        effect_id = f"import-operation/{op_id}/effect-observed"

        original_put = intel.put_artifact

        def failing_put(campaign_dir, campaign_id, campaign_revision, kind, artifact_id, payload):
            if artifact_id == effect_id:
                raise RuntimeError("simulated lifecycle persist failure")
            return original_put(campaign_dir, campaign_id, campaign_revision, kind, artifact_id, payload)

        intel.put_artifact = failing_put
        try:
            reader, calls = make_reader(
                lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
                campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
            )
            try:
                run_operation(cdir, data, reader=reader)
                raise AssertionError("first run must fail after observation persistence")
            except RuntimeError as exc:
                assert "simulated lifecycle persist failure" in str(exc)
        finally:
            intel.put_artifact = original_put

        assert artifact_file_count(cdir) == 2, "crash must leave exactly intent + observation"
        assert bkg._try_get_payload(cdir, data["id"], f"source-observation/{op_id}") is not None

        reader2, calls2 = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
        )
        result = run_operation(cdir, data, reader=reader2)
        assert result["status"] == "confirmed"
        assert len(calls2) == 0, "recovery must not reread the source"
        assert artifact_file_count(cdir) == 5
        assert bkg._try_get_payload(cdir, data["id"], effect_id) is not None
        observation_count = len([
            d for d in (cdir / "intelligence" / "artifacts").glob("*.json")
            if json.loads(d.read_text())["artifact_id"] == f"source-observation/{op_id}"
        ])
        assert observation_count == 1, "exactly one source observation artifact must exist"
        ok("intermediate recovery: intent+observation reused, reader not called, chain completed, exactly one observation")


def test_campaign_immutability() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-immut-") as td:
        cdir, data = init_campaign(td)
        cfile = cdir / "campaign.json"
        before_bytes = cfile.read_bytes()
        before_revision = data["revision"]
        before_wu = list(data["work_units"])
        before_dag = json.loads(json.dumps(data["dag"]))

        run_operation(cdir, data)
        run_operation(cdir, data)  # replay must also leave campaign.json untouched

        after_bytes = cfile.read_bytes()
        after_data = json.loads(after_bytes)
        assert after_bytes == before_bytes, "campaign.json must be byte-for-byte unchanged"
        assert after_data["revision"] == before_revision
        assert after_data["work_units"] == before_wu
        assert after_data["dag"] == before_dag
        ok("campaign.json bytes, revision, work_units, and dag identical before/after import")


def test_adapter_boundary_and_authority_flags() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-boundary-") as td:
        cdir, data = init_campaign(td)
        state: dict = {}
        reader, calls = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state=state,
        )
        result = run_operation(cdir, data, reader=reader, reader_state=state)
        assert len(calls) == 1
        assert set(calls[0]) == {"source_identity", "adapter", "request", "request_digest"}

        # Every persisted artifact carries bounded authority.
        for artifact_file in (cdir / "intelligence" / "artifacts").glob("*.json"):
            envelope = json.loads(artifact_file.read_text(encoding="utf-8"))
            authority = envelope["payload"]["authority"]
            assert authority["create_work_units"] is False, envelope["artifact_id"]
            assert authority["activate_plan"] is False, envelope["artifact_id"]
            assert authority["repository_write"] is False, envelope["artifact_id"]
            assert authority["commit"] is False and authority["push"] is False
            assert authority["external_read"] is True
        ok("adapter boundary: reader context has exactly allowed keys; every persisted authority is read-only")


def test_raw_content_rejection() -> None:
    raw_keys = ("source_code", "file_content", "raw_diff", "patch")
    for key in raw_keys:
        with tempfile.TemporaryDirectory(prefix="p1c-raw-") as td:
            cdir, data = init_campaign(td)
            request = {"issue": "INC-99", key: "def helper():\n    return 1"}
            reader, calls = make_reader(
                lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
                campaign_dir=cdir, campaign_id=data["id"], idempotency_key=f"raw-{key}", state={},
            )
            try:
                run_operation(cdir, data, request=request, reader=reader)
                raise AssertionError(f"request with {key} must be rejected")
            except bkg.ImportOperationInvalid:
                pass
            assert len(calls) == 0, f"raw {key} rejection must happen before reader invocation"
            assert artifact_file_count(cdir) == 0, "raw content rejection must precede intent artifact creation"
    ok(f"raw content rejection: {len(raw_keys)} keys rejected before reader invocation and before intent artifact")


def test_secret_rejection() -> None:
    secret_fixtures = [
        ("api_key", {"issue": "INC-1", "api_key": "0123456789abcdef"}),
        ("access_token", {"issue": "INC-1", "access_token": "sk-abcdef1234567890"}),
        ("bearer", {"issue": "INC-1", "authorization": "Bearer abcdefghijklmnop"}),
        ("private_key", {"issue": "INC-1", "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAA==\n-----END RSA PRIVATE KEY-----"}),
        ("nested-secret", {"issue": "INC-1", "config": {"client_secret": "s3cr3t-value-12345"}}),
    ]
    for label, request in secret_fixtures:
        with tempfile.TemporaryDirectory(prefix="p1c-secret-") as td:
            cdir, data = init_campaign(td)
            reader, calls = make_reader(
                lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
                campaign_dir=cdir, campaign_id=data["id"], idempotency_key=f"secret-{label}", state={},
            )
            try:
                run_operation(cdir, data, request=request, reader=reader)
                raise AssertionError(f"request with {label} must be rejected")
            except bkg.ImportOperationInvalid as exc:
                assert "secret/raw-content" in str(exc)
                assert "s3cr3t" not in str(exc) and "sk-abcdef" not in str(exc)
                assert "BEGIN RSA" not in str(exc)
            assert len(calls) == 0, f"secret {label} rejection must happen before reader invocation"
            assert artifact_file_count(cdir) == 0, "secret rejection must precede intent artifact creation"
    ok(f"secret rejection: {len(secret_fixtures)} fixtures rejected with no artifact, no reader call, value not echoed")


def test_lifecycle_chain_and_continuity() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-chain-") as td:
        cdir, data = init_campaign(td)
        result = run_operation(cdir, data)
        op_id = result["operation_id"]
        intent = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/intent")
        effect = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/effect-observed")
        receipt_recorded = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/receipt-recorded")
        confirmed = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/confirmed")
        observation = bkg._try_get_payload(cdir, data["id"], f"source-observation/{op_id}")

        # Lifecycle chain.
        assert bkg.validate_lifecycle_chain([intent, effect, receipt_recorded, confirmed]) == []
        assert intent["previous_record_id"] is None
        assert effect["previous_record_id"] == intent["record_id"]
        assert receipt_recorded["previous_record_id"] == effect["record_id"]
        assert confirmed["previous_record_id"] == receipt_recorded["record_id"]
        for record in (intent, effect, receipt_recorded, confirmed):
            for field in (
                "operation_id", "campaign_id", "source_identity", "adapter",
                "request_digest", "previous_content_digest", "authority",
            ):
                assert record[field] == intent[field], f"{record['status']} {field} differs"
            assert bkg.validate_import_record_semantics(record) == []

        # Continuity of the observation and receipt.
        obs_ref = {
            "observation_id": observation["observation_id"],
            "observation_digest": observation["observation_digest"],
            "outcome": observation["outcome"],
        }
        assert effect["observation"] == obs_ref
        assert receipt_recorded["observation"] == obs_ref
        assert confirmed["observation"] == obs_ref
        assert receipt_recorded["receipt"] == confirmed["receipt"]
        receipt = receipt_recorded["receipt"]
        assert bkg.validate_receipt_semantics(receipt) == []
        assert receipt["observation_id"] == observation["observation_id"]
        assert receipt["observation_digest"] == observation["observation_digest"]
        assert receipt["request_digest"] == intent["request_digest"]
        assert receipt["operation_id"] == intent["operation_id"]
        assert receipt["outcome"] == observation["outcome"]
        assert bkg.validate_source_observation_semantics(observation) == []
        assert result["observation_id"] == observation["observation_id"]
        assert result["confirmed_record_id"] == confirmed["record_id"]
        ok("lifecycle chain and observation/receipt continuity fully validated")


def test_mapping_counts_and_projection_descriptors() -> None:
    with tempfile.TemporaryDirectory(prefix="p1c-mapcount-") as td:
        cdir, data = init_campaign(td)
        run_operation(cdir, data)
        listing = intel.list_artifacts(cdir, data["id"])
        assert listing["status"] == "healthy"
        assert listing["artifact_count"] == 5, listing["artifact_count"]
        assert len(listing["artifacts"]["decision"]) == 4
        assert len(listing["artifacts"]["backlog"]) == 1
        decision_ids = {d["artifact_id"] for d in listing["artifacts"]["decision"]}
        op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
        assert decision_ids == {
            f"import-operation/{op_id}/intent",
            f"import-operation/{op_id}/effect-observed",
            f"import-operation/{op_id}/receipt-recorded",
            f"import-operation/{op_id}/confirmed",
        }
        assert {d["artifact_id"] for d in listing["artifacts"]["backlog"]} == {f"source-observation/{op_id}"}
        # Projection descriptors contain no lifecycle payloads.
        for descriptor in listing["artifacts"]["decision"] + listing["artifacts"]["backlog"]:
            assert set(descriptor) == {"artifact_id", "artifact_digest", "payload_digest", "created_at", "storage_key"}
            assert "payload" not in descriptor
        ok("mapping: exactly 4 decision + 1 backlog artifact; projection descriptors carry no lifecycle payloads")


# ---------------------------------------------------------------------------
# WU-P1-003-C corrective regression tests (frontier remote audit turn 42)
# ---------------------------------------------------------------------------

def test_persisted_record_corruption() -> None:
    """Regression group 1: runtime corruption bypassing the immutable APIs.

    The stored P1-003 payload is modified directly on disk and the outer P1-002
    envelope digests recomputed so the envelope stays internally valid; replay
    must reject the corrupted P1-003 contract before extending/returning the
    chain.
    """
    # 1a: unknown top-level field on the stored intent -> semantic rejection.
    with tempfile.TemporaryDirectory(prefix="p1c-corr-intent-") as td:
        cdir, data = init_campaign(td)
        run_operation(cdir, data)
        op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
        artifact_id = f"import-operation/{op_id}/intent"
        files_before = artifact_file_count(cdir)

        def add_unknown_field(payload):
            payload["sneaky_field"] = 1
            payload["record_digest"] = bkg.compute_record_digest(payload)
            payload["record_id"] = bkg.derive_record_id(payload["record_digest"])

        tamper_artifact(cdir, data["id"], artifact_id, add_unknown_field)
        # The P1-002 envelope itself is still valid.
        assert intel.get_artifact(cdir, data["id"], artifact_id)["ok"] is True
        reader, calls = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
        )
        try:
            run_operation(cdir, data, reader=reader)
            raise AssertionError("tampered intent must be rejected on replay")
        except bkg.ImportOperationInvalid:
            pass
        assert len(calls) == 0, "corrupted intent must fail before the reader"
        assert artifact_file_count(cdir) == files_before

    # 1b: identity-bearing tamper on the stored intent -> conflict rejection.
    with tempfile.TemporaryDirectory(prefix="p1c-corr-intent2-") as td:
        cdir, data = init_campaign(td)
        run_operation(cdir, data)
        op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
        artifact_id = f"import-operation/{op_id}/intent"
        files_before = artifact_file_count(cdir)

        def change_identity(payload):
            payload["source_identity"] = {"kind": "github", "canonical_id": "other/repo"}
            payload["record_digest"] = bkg.compute_record_digest(payload)
            payload["record_id"] = bkg.derive_record_id(payload["record_digest"])

        tamper_artifact(cdir, data["id"], artifact_id, change_identity)
        assert intel.get_artifact(cdir, data["id"], artifact_id)["ok"] is True
        reader, calls = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
        )
        try:
            run_operation(cdir, data, reader=reader)
            raise AssertionError("identity-tampered intent must conflict on replay")
        except bkg.ImportOperationConflict:
            pass
        assert len(calls) == 0, "identity-tampered intent must conflict before the reader"
        assert artifact_file_count(cdir) == files_before
    ok("persisted-record corruption: 2 envelope-internally-valid tamper scenarios rejected before extending/returning")


def test_source_observation_continuity_corruption() -> None:
    """Regression group 2: source-observation continuity corruption.

    Independently tamper source_identity, adapter, previous_content_digest,
    authority.create_work_units=true, authority.activate_plan=true, and an
    unknown top-level field on the stored observation; each replay must raise
    ImportOperationInvalid and the reader must NOT be invoked during recovery.
    """
    cases = [
        ("source_identity", lambda p: p.update({"source_identity": {"kind": "github", "canonical_id": "other/repo"}})),
        ("adapter", lambda p: p.update({"adapter": "github"})),
        ("previous_content_digest", lambda p: p.update({"previous_content_digest": HEX64_C})),
        ("authority-create-work-units", lambda p: p["authority"].update({"create_work_units": True})),
        ("authority-activate-plan", lambda p: p["authority"].update({"activate_plan": True})),
        ("unknown-field", lambda p: p.update({"sneaky_field": 1})),
    ]
    for label, mutate in cases:
        with tempfile.TemporaryDirectory(prefix="p1c-corr-obs-") as td:
            cdir, data = init_campaign(td)
            op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
            run_operation_until(cdir, data, f"import-operation/{op_id}/effect-observed")
            assert artifact_file_count(cdir) == 2, "intermediate state must be intent + observation"
            artifact_id = f"source-observation/{op_id}"
            files_before = artifact_file_count(cdir)

            def recompute(payload):
                mutate(payload)
                payload["observation_digest"] = bkg.compute_observation_digest(payload)
                payload["observation_id"] = bkg.derive_observation_id(payload["observation_digest"])

            tamper_artifact(cdir, data["id"], artifact_id, recompute)
            reader, calls = make_reader(
                lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
                campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
            )
            try:
                run_operation(cdir, data, reader=reader)
                raise AssertionError(f"tampered observation ({label}) must be rejected")
            except bkg.ImportOperationInvalid:
                pass
            assert len(calls) == 0, f"reader must not be invoked during recovery ({label})"
            assert artifact_file_count(cdir) == files_before
    ok(f"source-observation continuity corruption: {len(cases)} tamper scenarios -> ImportOperationInvalid, reader not invoked")


def test_lifecycle_record_continuity_corruption() -> None:
    """Regression group 3: lifecycle-record continuity corruption.

    Tamper the existing effect-observed record with recomputed inner record and
    outer sidecar digests for divergent source_identity/adapter/request/
    previous_content_digest/authority/operation identity and an unknown
    top-level field; replay must fail before receipt/confirmation extension.
    """
    cases = [
        ("source_identity", lambda p: p.update({"source_identity": {"kind": "github", "canonical_id": "other/repo"}})),
        ("adapter", lambda p: p.update({"adapter": "github"})),
        ("request", lambda p: (
            p.update({"request": {"issue": "INC-999"}}),
            p.update({"request_digest": bkg.compute_request_digest(p["request"])}),
        )),
        ("previous_content_digest", lambda p: p.update({"previous_content_digest": HEX64_C})),
        ("authority", lambda p: p.update({"authority": {**dict(bkg.IMPORT_AUTHORITY), "create_work_units": True}})),
        ("operation_identity", lambda p: p.update({"operation_id": "importop-" + "22" * 12})),
        ("unknown-field", lambda p: p.update({"sneaky_field": 1})),
    ]
    for label, mutate in cases:
        with tempfile.TemporaryDirectory(prefix="p1c-corr-effect-") as td:
            cdir, data = init_campaign(td)
            op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
            run_operation_until(cdir, data, f"import-operation/{op_id}/receipt-recorded")
            assert artifact_file_count(cdir) == 3, "intermediate state must be intent + observation + effect-observed"
            artifact_id = f"import-operation/{op_id}/effect-observed"
            files_before = artifact_file_count(cdir)

            def recompute(payload):
                mutate(payload)
                payload["record_digest"] = bkg.compute_record_digest(payload)
                payload["record_id"] = bkg.derive_record_id(payload["record_digest"])

            tamper_artifact(cdir, data["id"], artifact_id, recompute)
            reader, calls = make_reader(
                lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
                campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
            )
            try:
                run_operation(cdir, data, reader=reader)
                raise AssertionError(f"tampered effect-observed ({label}) must be rejected")
            except bkg.ImportOperationInvalid:
                pass
            assert len(calls) == 0, f"reader must not be invoked during recovery ({label})"
            assert artifact_file_count(cdir) == files_before
            assert bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/receipt-recorded") is None
            assert bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/confirmed") is None
    ok(f"lifecycle-record continuity corruption: {len(cases)} tamper scenarios rejected before receipt/confirmation extension")


def test_confirmed_chain_corruption() -> None:
    """Regression group 4: confirmed-chain corruption.

    Tamper receipt-recorded and confirmed independently with recomputed inner
    and outer digests; no confirmed result is returned and no new artifact is
    written; the corrupted chain is rejected.
    """
    # 4a: receipt-recorded receipt tampered (internally consistent).
    with tempfile.TemporaryDirectory(prefix="p1c-corr-receipt-") as td:
        cdir, data = init_campaign(td)
        run_operation(cdir, data)
        op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
        artifact_id = f"import-operation/{op_id}/receipt-recorded"
        files_before = artifact_file_count(cdir)

        def tamper_receipt(payload):
            payload["receipt"]["outcome"] = "unavailable"
            payload["receipt"]["receipt_digest"] = bkg.compute_receipt_digest(payload["receipt"])
            payload["receipt"]["receipt_id"] = bkg.derive_receipt_id(payload["receipt"]["receipt_digest"])
            payload["record_digest"] = bkg.compute_record_digest(payload)
            payload["record_id"] = bkg.derive_record_id(payload["record_digest"])

        tamper_artifact(cdir, data["id"], artifact_id, tamper_receipt)
        try:
            run_operation(cdir, data)
            raise AssertionError("tampered receipt-recorded must reject the confirmed replay")
        except bkg.ImportOperationInvalid:
            pass
        assert artifact_file_count(cdir) == files_before, "no new artifact may be written"

    # 4b: confirmed observation reference tampered (internally consistent).
    with tempfile.TemporaryDirectory(prefix="p1c-corr-confirmed-") as td:
        cdir, data = init_campaign(td)
        run_operation(cdir, data)
        op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
        artifact_id = f"import-operation/{op_id}/confirmed"
        files_before = artifact_file_count(cdir)

        def tamper_confirmed(payload):
            payload["observation"]["observation_digest"] = HEX64_C
            payload["observation"]["observation_id"] = "srcobs_" + HEX64_C
            payload["record_digest"] = bkg.compute_record_digest(payload)
            payload["record_id"] = bkg.derive_record_id(payload["record_digest"])

        tamper_artifact(cdir, data["id"], artifact_id, tamper_confirmed)
        try:
            run_operation(cdir, data)
            raise AssertionError("tampered confirmed must be rejected")
        except bkg.ImportOperationInvalid:
            pass
        assert artifact_file_count(cdir) == files_before, "no new artifact may be written"
    ok("confirmed-chain corruption: receipt-recorded and confirmed tamper scenarios rejected; no result, no new artifact")


def test_mutating_reader_regression() -> None:
    """Regression group 5: malicious/mutating-reader.

    The reader receives private defensive copies of the authoritative inputs;
    mutating them must not affect the durable intent, later builders, the
    caller's objects, or the request digest. The operation completes with the
    original authoritative source/request and every persisted record validates.
    """
    with tempfile.TemporaryDirectory(prefix="p1c-mutreader-") as td:
        cdir, data = init_campaign(td)
        caller_source_identity = dict(SOURCE_IDENTITY)
        caller_request = json.loads(json.dumps(RUNTIME_REQUEST))
        original_si = json.loads(json.dumps(caller_source_identity))
        original_req = json.loads(json.dumps(caller_request))
        state: dict = {}

        def mutating_reader(ctx: dict) -> dict:
            # State-first: durable intent exists and matches authoritative inputs.
            op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
            intent_payload = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/intent")
            assert intent_payload is not None, "intent must be durably persisted before reader runs"
            assert intent_payload["source_identity"] == SOURCE_IDENTITY
            assert intent_payload["request"] == RUNTIME_REQUEST
            # Mutate the reader's private context copies aggressively.
            ctx["source_identity"]["canonical_id"] = "MUTATED-ORIGIN"
            ctx["source_identity"]["nested_mutation"] = {"hacked": True}
            ctx["request"]["issue"] = "MUTATED-REQ"
            ctx["request"]["nested"] = {"injected": True}
            state["reader_ctx_ref"] = ctx
            state["reader_mutated"] = True
            return {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D}

        result = run_operation(
            cdir, data, source_identity=caller_source_identity, request=caller_request,
            reader=mutating_reader, reader_state=state,
        )
        assert result["status"] == "confirmed"
        assert state.get("reader_mutated") is True
        # The reader's mutations stayed in its private context copy.
        reader_ctx = state["reader_ctx_ref"]
        assert reader_ctx["source_identity"]["canonical_id"] == "MUTATED-ORIGIN"
        assert reader_ctx["request"]["issue"] == "MUTATED-REQ"
        # Caller-provided objects were NOT mutated.
        assert caller_source_identity == original_si
        assert caller_request == original_req

        op_id = result["operation_id"]
        intent = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/intent")
        effect = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/effect-observed")
        receipt_recorded = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/receipt-recorded")
        confirmed = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/confirmed")
        observation = bkg._try_get_payload(cdir, data["id"], f"source-observation/{op_id}")
        expected_digest = bkg.compute_request_digest(RUNTIME_REQUEST)
        for record in (intent, effect, receipt_recorded, confirmed):
            assert record["source_identity"] == SOURCE_IDENTITY, "lifecycle record source_identity diverged"
            assert record["request"] == RUNTIME_REQUEST, "lifecycle record request diverged"
            assert record["request_digest"] == expected_digest, "lifecycle record request_digest diverged"
            assert bkg.validate_import_record_semantics(record) == []
            assert bkg._intent_mismatch_errors(record, intent) == []
        assert observation["source_identity"] == SOURCE_IDENTITY
        assert observation["request_digest"] == expected_digest
        assert bkg.validate_source_observation_semantics(observation) == []
        assert bkg.validate_lifecycle_chain([intent, effect, receipt_recorded, confirmed]) == []
        assert result["request_digest"] == expected_digest
    ok("mutating-reader: private-context mutations isolated; operation completes with original authoritative source/request; caller objects unmutated; all persisted records validate")


def test_reader_exception_after_mutation_regression() -> None:
    """Regression group 6: mutation-plus-exception.

    A reader that mutates its private context and then raises an unexpected
    exception leaves the durable intent valid and unchanged; no observation,
    effect, receipt, or confirmed artifact exists; replay with a valid reader
    continues from the original intent.
    """
    with tempfile.TemporaryDirectory(prefix="p1c-mutexc-") as td:
        cdir, data = init_campaign(td)
        state: dict = {}

        def bad_reader(ctx: dict) -> dict:
            ctx["source_identity"]["canonical_id"] = "MUTATED-ORIGIN"
            ctx["request"]["issue"] = "MUTATED-REQ"
            state["reader_mutated"] = True
            raise RuntimeError("boom after mutation")

        try:
            run_operation(cdir, data, reader=bad_reader, reader_state=state)
            raise AssertionError("reader exception must escape")
        except RuntimeError as exc:
            assert "boom after mutation" in str(exc)
        assert state.get("reader_mutated") is True

        op_id = bkg.derive_operation_id(data["id"], RUNTIME_IDEMPOTENCY)
        intent = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/intent")
        assert intent is not None
        assert bkg.validate_import_record_semantics(intent) == []
        assert intent["source_identity"] == SOURCE_IDENTITY
        assert intent["request"] == RUNTIME_REQUEST
        assert intent["request_digest"] == bkg.compute_request_digest(RUNTIME_REQUEST)
        # No downstream artifact exists.
        assert artifact_file_count(cdir) == 1, "only the intent may be durable"
        assert bkg._try_get_payload(cdir, data["id"], f"source-observation/{op_id}") is None
        for stage in ("effect-observed", "receipt-recorded", "confirmed"):
            assert bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/{stage}") is None

        # Replay with a valid reader continues from the original intent.
        reader2, calls2 = make_reader(
            lambda: {"status": "observed", "observed_version": "v7", "content_digest": HEX64_D},
            campaign_dir=cdir, campaign_id=data["id"], idempotency_key=RUNTIME_IDEMPOTENCY, state={},
        )
        result = run_operation(cdir, data, reader=reader2)
        assert result["status"] == "confirmed"
        assert len(calls2) == 1
        effect = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/effect-observed")
        confirmed = bkg._try_get_payload(cdir, data["id"], f"import-operation/{op_id}/confirmed")
        assert effect["source_identity"] == SOURCE_IDENTITY
        assert effect["request"] == RUNTIME_REQUEST
        assert confirmed["request_digest"] == bkg.compute_request_digest(RUNTIME_REQUEST)
        for record in (intent, effect, confirmed):
            assert bkg.validate_import_record_semantics(record) == []
    ok("mutation-plus-exception: durable intent unchanged and valid; no downstream artifact; replay continues from original intent")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_schema_meta_and_identity,
        test_positive_schema_fixtures,
        test_negative_schema_fixtures,
        test_semantic_digest_derivations,
        test_state_first_ordering,
        test_changed_unchanged_mapping,
        test_timeout_maps_to_unavailable,
        test_invalid_and_ambiguous_preserved,
        test_idempotent_replay,
        test_negative_replay_conflict,
        test_identity_conflicts,
        test_intermediate_recovery,
        test_campaign_immutability,
        test_adapter_boundary_and_authority_flags,
        test_raw_content_rejection,
        test_secret_rejection,
        test_lifecycle_chain_and_continuity,
        test_mapping_counts_and_projection_descriptors,
        # WU-P1-003-C corrective regression groups.
        test_persisted_record_corruption,
        test_source_observation_continuity_corruption,
        test_lifecycle_record_continuity_corruption,
        test_confirmed_chain_corruption,
        test_mutating_reader_regression,
        test_reader_exception_after_mutation_regression,
    ]
    for test in tests:
        test()
    print(f"assertion_checks: {TEST_COUNT} ok checks, 0 failed")
    print(f"positive_fixture_results: source-observation=6 import-operation=4 passed")
    print(f"negative_fixture_results: source-observation={len(_source_negative_fixtures())} import-operation={len(_import_negative_fixtures())} passed")
    print("regression_corrective_results:")
    print("  persisted-record corruption: 2 scenarios rejected (envelope-internally-valid tamper)")
    print("  authority corruption (source-observation continuity): 6 scenarios -> ImportOperationInvalid, reader not invoked")
    print("  immutable-purpose continuity (lifecycle-record corruption): 7 scenarios rejected before receipt/confirmation extension")
    print("  confirmed-chain corruption: 2 scenarios rejected; no result, no new artifact")
    print("  mutating reader: 1 regression (private-context mutations isolated)")
    print("  exception-after-mutation recovery: 1 regression (durable intent unchanged, replay continues)")
    print("source-import contract tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
