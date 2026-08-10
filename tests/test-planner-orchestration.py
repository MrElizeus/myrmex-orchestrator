#!/usr/bin/env python3
"""Focused P1-008 integrity, replay, recovery, and capability checks."""
from __future__ import annotations
import copy, json, pathlib, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_backlog_normalizer as backlog
import myrmex_campaign_intelligence as intel
import myrmex_plan_store as store
import myrmex_planner as planner

failures = []
def check(value, message):
    if not value: failures.append(message)
def raises(exc, fn, message):
    try: fn()
    except exc: return
    except Exception as error: failures.append(f"{message}: {type(error).__name__}")
    else: failures.append(f"{message}: no failure")

def fixture():
    root = pathlib.Path(tempfile.mkdtemp()); cid = "camp-p1008-test"
    identity = {"kind": "local-roadmap", "canonical_id": "roadmap.md"}; entity = "srcitem_" + "a" * 64
    item = {"schema": backlog.NORMALIZED_ITEM_SCHEMA, "backlog_item_id": "", "item_digest": "", "source_adapter": "local-markdown-roadmap/v1", "source_identity": identity, "source_entity_type": "local-item", "source_entity_id": entity, "title": "plan this", "priority": None, "state": None, "dependency_hints": [], "constraints": [], "context_constraints": [], "labels": [], "group_ref": None}
    item["backlog_item_id"] = backlog.compute_backlog_item_id(item["source_adapter"], identity, entity); item["item_digest"] = backlog.compute_item_digest(item)
    item_id = f"normalized-backlog/item/{item['backlog_item_id']}/{item['item_digest']}"; intel.put_artifact(root, cid, 1, "backlog", item_id, item)
    source_digest = "c" * 64
    source = {"operation_id":"importop-" + "c" * 24, "observation_id":"srcobs_" + source_digest, "observation_digest":source_digest, "request_digest":"d" * 64, "content_digest":"e" * 64, "outcome":"changed", "adapter":"local-markdown-roadmap/v1", "source_identity":identity}
    snap = {"schema": backlog.NORMALIZED_SNAPSHOT_SCHEMA, "snapshot_record_id": "", "snapshot_record_digest": "", "snapshot_digest": "", "source_count": 1, "sources": [source], "item_count": 1, "items":[{"backlog_item_id":item["backlog_item_id"],"item_digest":item["item_digest"],"artifact_id":item_id}]}
    snap["snapshot_digest"] = backlog.compute_snapshot_digest_from_snapshot(snap); snap["snapshot_record_digest"] = backlog.compute_snapshot_record_digest(snap); snap["snapshot_record_id"] = "blsnaprec_" + snap["snapshot_record_digest"]
    intel.put_artifact(root, cid, 1, "backlog", "normalized-backlog/snapshot/" + snap["snapshot_record_id"], snap)
    constraints = {"allowed_paths": ["src/"], "forbidden_paths": [".git"], "required_invariants": ["planning-only"], "required_sections": ["work_units"]}
    req = planner.create_planning_request(root, cid, 1, "req-p1008", "run-p1008", "obj-p1008", "0" * 40, snap["snapshot_record_id"], constraints)
    return root, cid, req, snap, item

def make_result(req, kind="already_complete"):
    result = {"schema": planner.RESULT_SCHEMA, "request_id": req["request_id"], "run_id": req["run_id"], "campaign_id": req["campaign_id"], "objective_id": req["objective_id"], "base_sha": req["base_sha"], "response_type": kind, "plan_revision": None, "clarification": None, "completion_evidence": ["evidence"] if kind == "already_complete" else [], "authority": dict(planner.AUTHORITY), "result_digest": "", "created_at": "2026-08-09T00:00:00+00:00"}
    result["result_digest"] = planner._sha({k:v for k,v in result.items() if k != "result_digest"}); return result
def refresh(result):
    result["result_digest"] = planner._sha({k:v for k,v in result.items() if k != "result_digest"}); return result

def make_wu():
    return {"id":"WU-P1-008", "objective":"bounded planning", "non_goals":["activation"], "dependencies":[], "scope":{"allowed_paths":["src/"],"forbidden_paths":[".git"]}, "acceptance_criteria":["strict validation"], "verification":{"commands":[],"manual_checks":[],"discover_when_missing":False}, "risk_class":"bounded", "required_route":"frontier-gated", "human_gates":[], "required_evidence":[], "terminal_gate":"proposal"}

def make_plan(req):
    plan = {"schema":"myrmex.plan-revision/v1", "record_id":"", "plan_revision_id":"", "campaign_id":req["campaign_id"], "objective_id":req["objective_id"], "planning_request_id":req["request_id"], "base_sha":req["base_sha"], "parent_revision":None, "input_digests":copy.deepcopy(req["input_digests"]), "assumptions":[], "work_units":[make_wu()], "edges":[], "lifecycle_status":"proposed", "previous_record_id":None, "plan_digest":"", "record_digest":"", "created_at":"2026-08-09T00:00:00+00:00"}
    plan["plan_digest"] = store.compute_plan_digest(plan); plan["plan_revision_id"] = store.derive_plan_revision_id(plan["plan_digest"]); plan["record_digest"] = store.compute_record_digest(plan); plan["record_id"] = store.derive_record_id(plan["record_digest"])
    return plan

root, cid, req, snap, item = fixture()
replay = planner.create_planning_request(root, cid, 1, req["request_id"], req["run_id"], req["objective_id"], req["base_sha"], snap["snapshot_record_id"], req["constraints"])
check(replay == req and replay["created_at"] == planner.DEFAULT_CREATED_AT, "stable omitted timestamp replay")
raises(planner.PlanningRequestConflict, lambda: planner.create_planning_request(root, cid, 1, req["request_id"], req["run_id"], req["objective_id"], req["base_sha"], snap["snapshot_record_id"], req["constraints"], "2026-08-09T00:00:00+00:00"), "timestamp conflict")
raises(planner.PlanningRequestInvalid, lambda: planner.create_planning_request(root, cid, 1, "request-invalid-time", req["run_id"], req["objective_id"], req["base_sha"], snap["snapshot_record_id"], req["constraints"], "2026-08-09T00:00:00"), "request timezone required")
raises(planner.PlanningRequestInvalid, lambda: planner.create_planning_request(root, cid, 1, "r" * 257, req["run_id"], req["objective_id"], req["base_sha"], snap["snapshot_record_id"], req["constraints"]), "request ID bounded")
context = planner.build_planning_context(root, cid, req["request_id"]); prompt = planner.render_planning_prompt(context)
check(prompt == planner.render_planning_prompt(copy.deepcopy(context)), "deterministic prompt")
mutations = [(lambda c: c.update(extra=1), "context extra"), (lambda c: c["normalized_backlog"]["items"][0].update(secret="x"), "context secret"), (lambda c: c["normalized_backlog"]["items"][0].update(item_digest="0"*64), "context digest"), (lambda c: c["normalized_backlog"]["snapshot"].update(snapshot_digest="0"*64), "context snapshot")]
for mutate, label in mutations:
    bad = copy.deepcopy(context); mutate(bad); raises(planner.PlanningInputInvalid, lambda bad=bad: planner.render_planning_prompt(bad), label)
bad_context_request = copy.deepcopy(context); bad_context_request["request"]["input_digests"][0]["sha256"] = "0" * 64
raises(planner.PlanningInputInvalid, lambda: planner.render_planning_prompt(bad_context_request), "request-bound snapshot digest")

complete = make_result(req); planner.validate_planning_result(req, complete); planner.record_planning_result(root, cid, 1, req["request_id"], complete); planner.record_planning_result(root, cid, 1, req["request_id"], complete)
clar = make_result(req, "blocking_clarification"); clar["clarification"] = {"question":"choose", "options":["a"], "recommended_default":None}; clar["result_digest"] = planner._sha({k:v for k,v in clar.items() if k != "result_digest"}); planner.validate_planning_result(req, clar)
for mutate, label in [(lambda r: r["authority"].update(commit_authorized=True), "authority"), (lambda r: r.update(result_digest="0"*64), "digest"), (lambda r: r.update(completion_evidence=[]), "completion evidence")]:
    bad = copy.deepcopy(complete); mutate(bad); bad["result_digest"] = planner._sha({k:v for k,v in bad.items() if k != "result_digest"}) if label != "digest" else bad["result_digest"]; raises(planner.PlanningResultInvalid, lambda bad=bad: planner.validate_planning_result(req, bad), label)
bad_time = copy.deepcopy(complete); bad_time["created_at"] = "2026-08-09T00:00:00"; refresh(bad_time)
raises(planner.PlanningResultInvalid, lambda: planner.validate_planning_result(req, bad_time), "result timezone required")
missing = fixture(); raises(planner.PlanningInputInvalid, lambda: planner.build_planning_context(missing[0], missing[1], "absent"), "missing request")
(root / "intelligence" / "projection.json").unlink(); check(planner.build_planning_context(root, cid, req["request_id"])["schema"] == planner.CONTEXT_SCHEMA, "projection recovery")
source = pathlib.Path(planner.__file__).read_text(encoding="utf-8")
for forbidden in ("import requests", "import urllib", "import subprocess", "import socket", "import httpx", "import aiohttp"): check(forbidden not in source, forbidden)
check(not (root / "campaign.json").exists(), "campaign unchanged")
root_corrupt, cid_corrupt, req_corrupt, snap_corrupt, _ = fixture()
request_path = root_corrupt / "intelligence" / "artifacts" / (intel.artifact_storage_key("planning-request/request/" + planner._text_sha(req_corrupt["request_id"])) + ".json")
request_envelope = json.loads(request_path.read_text(encoding="utf-8")); request_envelope["payload"]["unexpected"] = True; request_path.write_text(json.dumps(request_envelope), encoding="utf-8")
raises(planner.PlanningInputInvalid, lambda: planner.build_planning_context(root_corrupt,cid_corrupt,req_corrupt["request_id"]), "corrupt authoritative request")
raises(planner.PlanningRequestConflict, lambda: planner.create_planning_request(root_corrupt, cid_corrupt, 1, req_corrupt["request_id"], req_corrupt["run_id"], req_corrupt["objective_id"], req_corrupt["base_sha"], snap_corrupt["snapshot_record_id"], req_corrupt["constraints"]), "corrupt request replay fails closed")

# A valid plan persists response first, then exactly one proposed P1-007 root.
root_plan, cid_plan, req_plan, _, _ = fixture()
plan_result = make_result(req_plan, "plan"); plan_result["plan_revision"] = make_plan(req_plan); plan_result["result_digest"] = planner._sha({k:v for k,v in plan_result.items() if k != "result_digest"})
receipt = planner.record_planning_result(root_plan, cid_plan, 1, req_plan["request_id"], plan_result)
check(receipt["lifecycle_status"] == "proposed", "proposed plan persisted")
response_id = "planning-result/response/" + planner._text_sha(req_plan["request_id"])
check(intel.get_artifact(root_plan, cid_plan, response_id)["artifact"]["payload"] == plan_result, "response artifact persisted")
check(len(store._list_plan_record_envelopes(root_plan, cid_plan)) == 1, "one proposed plan record")
check(planner.record_planning_result(root_plan, cid_plan, 1, req_plan["request_id"], plan_result)["status"] == "reused", "exact plan result replay")
conflict_result = copy.deepcopy(plan_result); conflict_result["created_at"] = "2026-08-10T00:00:00+00:00"; refresh(conflict_result)
raises(planner.PlanningResultConflict, lambda: planner.record_planning_result(root_plan, cid_plan, 1, req_plan["request_id"], conflict_result), "conflicting result replay")
check(len(store._list_plan_record_envelopes(root_plan, cid_plan)) == 1, "conflict cannot mutate plan store")
response_path = root_plan / "intelligence" / "artifacts" / (intel.artifact_storage_key(response_id) + ".json")
response_envelope = json.loads(response_path.read_text(encoding="utf-8")); response_envelope["payload"]["unexpected"] = True; response_path.write_text(json.dumps(response_envelope), encoding="utf-8")
raises(planner.PlanningResultConflict, lambda: planner.record_planning_result(root_plan, cid_plan, 1, req_plan["request_id"], plan_result), "corrupt response fail closed")

# Fault injection proves response-before-plan ordering and recovery.
root2, cid2, req2, snap2, _ = fixture(); plan_result2 = make_result(req2, "plan"); plan_result2["plan_revision"] = make_plan(req2); plan_result2["result_digest"] = planner._sha({k:v for k,v in plan_result2.items() if k != "result_digest"})
original_store = planner.plan_store.store_plan_record
planner.plan_store.store_plan_record = lambda *args: (_ for _ in ()).throw(RuntimeError("injected plan-store crash"))
raises(planner.PlanningResultConflict, lambda: planner.record_planning_result(root2, cid2, 1, req2["request_id"], plan_result2), "response-before-plan crash")
planner.plan_store.store_plan_record = original_store
check(intel.get_artifact(root2, cid2, "planning-result/response/" + planner._text_sha(req2["request_id"]))["artifact"]["payload"] == plan_result2, "lost acknowledgement response durable")
check(planner.record_planning_result(root2, cid2, 1, req2["request_id"], plan_result2)["status"] == "created", "recovery completes plan exactly once")
check(len(store._list_plan_record_envelopes(root2, cid2)) == 1, "recovery has one plan")

# Closed malformed-result matrix, including embedded plan structure and lifecycle.
for mutate, label in [(lambda r:r.pop("authority"),"missing field"), (lambda r:r.update(extra=1),"extra field"), (lambda r:r.update(schema="wrong"),"schema"), (lambda r:r.update(request_id="other"),"request"), (lambda r:r.update(run_id="other"),"run"), (lambda r:r.update(campaign_id="camp-other"),"campaign"), (lambda r:r.update(objective_id="other"),"objective"), (lambda r:r.update(base_sha="1"*40),"base"), (lambda r:r["authority"].update(scope="execution"),"scope")]:
    bad = make_result(req); mutate(bad); refresh(bad); raises(planner.PlanningResultInvalid if label in ("missing field","extra field","scope") else planner.PlanningResultMismatch, lambda bad=bad: planner.validate_planning_result(req,bad), label)
for mutate, label in [(lambda p:p.update(lifecycle_status="reviewed"),"lifecycle"), (lambda p:p.update(previous_record_id="planrec_"+"a"*64),"previous record"), (lambda p:p.update(parent_revision={"artifact_id":"x","artifact_digest":"a"*64}),"parent revision"), (lambda p:p["work_units"][0].update(id="bad"),"WU ID"), (lambda p:p.update(record_digest="0"*64),"plan digest")]:
    bad = make_result(req,"plan"); bad["plan_revision"] = make_plan(req); mutate(bad["plan_revision"]); refresh(bad); raises(planner.PlanningResultInvalid, lambda bad=bad: planner.validate_planning_result(req,bad), label)
bad = make_result(req,"plan"); bad["plan_revision"] = make_plan(req); bad["completion_evidence"]=["unexpected"]; refresh(bad); raises(planner.PlanningResultInvalid, lambda: planner.validate_planning_result(req,bad), "plan completion evidence")
bad = make_result(req,"blocking_clarification"); bad["clarification"]={"question":"q","options":["a"],"recommended_default":None}; bad["completion_evidence"]=["e"]; refresh(bad); raises(planner.PlanningResultInvalid, lambda: planner.validate_planning_result(req,bad), "clarification evidence")
bad = make_result(req,"already_complete"); bad["plan_revision"]={}; refresh(bad); raises(planner.PlanningResultInvalid, lambda: planner.validate_planning_result(req,bad), "completion plan")

# Authoritative missing artifacts fail closed; projection repair remains safe.
root3, cid3, req3, snap3, item3 = fixture(); item_path = root3 / "intelligence" / "artifacts" / (intel.artifact_storage_key(f"normalized-backlog/item/{item3['backlog_item_id']}/{item3['item_digest']}") + ".json"); item_path.unlink()
raises(planner.PlanningInputInvalid, lambda: planner.build_planning_context(root3,cid3,req3["request_id"]), "missing authoritative item")
root4, cid4, req4, snap4, _ = fixture(); (root4 / "intelligence" / "artifacts" / (intel.artifact_storage_key("normalized-backlog/snapshot/"+snap4["snapshot_record_id"])+".json")).unlink()
raises(planner.PlanningInputInvalid, lambda: planner.build_planning_context(root4,cid4,req4["request_id"]), "missing authoritative snapshot")
if failures: raise SystemExit("planner orchestration failures: " + "; ".join(failures))
print("planner orchestration: expanded integrity/replay/recovery assertions passed")
