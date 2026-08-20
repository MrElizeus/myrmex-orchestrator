#!/usr/bin/env python3
"""P2 portfolio candidate coordination, capacity, replay, and read-only tests."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/myrmex-campaign"
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_portfolio_scheduler as portfolio  # noqa: E402


def run_campaign(args: list[str], state_home: pathlib.Path, ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("command unexpectedly succeeded: " + " ".join(args))
    return proc


def tree_bytes(root: pathlib.Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def wu(wuid: str, *, dependencies: list[str] | None = None, status: str = "pending", created_at: str = "2026-08-10T10:00:00+00:00") -> dict:
    return {
        "id": wuid,
        "created_at": created_at,
        "status": status,
        "phase": "completed" if status == "completed" else "ready",
        "dependencies": list(dependencies or []),
        "corrections_used": 0,
        "corrections_budget": 3,
        "blocker": None,
        "required_route": "direct-only",
    }


def write_campaign(state_home: pathlib.Path, campaign_id: str, work_units: list[dict], revision: int = 7) -> pathlib.Path:
    run_campaign([
        "init", "--id", campaign_id, "--title", "Portfolio fixture", "--objective", "Candidate coordination",
        "--repo-root", str(state_home),
    ], state_home)
    campaign_dir = state_home / "myrmex" / "campaigns" / campaign_id
    path = campaign_dir / "campaign.json"
    campaign = json.loads(path.read_text(encoding="utf-8"))
    campaign["revision"] = revision
    campaign["created_at"] = "2026-08-01T00:00:00+00:00"
    campaign["updated_at"] = "2026-08-10T12:00:00+00:00"
    campaign["work_units"] = copy.deepcopy(work_units)
    campaign["dag"]["edges"] = sorted([
        [dependency, item["id"]]
        for item in work_units
        for dependency in item["dependencies"]
    ])
    campaign["active_work_unit"] = None
    path.write_text(json.dumps(campaign, indent=2) + "\n", encoding="utf-8")
    return campaign_dir


with tempfile.TemporaryDirectory(prefix="myrmex-p2-portfolio-") as td:
    root = pathlib.Path(td)
    state_home = root / "state"
    campaign_a = write_campaign(
        state_home, "camp-portfolio-a",
        [wu("WU-A"), wu("WU-A-OTHER"), wu("WU-A-CHILD", dependencies=["WU-A"])],
    )
    campaign_b = write_campaign(
        state_home, "camp-portfolio-b",
        [wu("WU-B")],
    )
    campaigns_root = state_home / "myrmex" / "campaigns"
    before = tree_bytes(campaigns_root)

    decision = portfolio.preview_portfolio(campaigns_root, max_concurrent_wu=2)
    portfolio.validate_portfolio_decision(decision)
    import jsonschema
    jsonschema.validate(
        decision,
        json.loads((ROOT / "contracts/portfolio-scheduling-decision-v1.schema.json").read_text(encoding="utf-8")),
    )
    assert tree_bytes(campaigns_root) == before
    assert decision["authority"] == portfolio.AUTHORITY
    assert decision["campaigns"] == sorted(decision["campaigns"], key=lambda item: item["campaign_id"])
    assert decision["eligible_candidate_ids"] == [
        "camp-portfolio-a/WU-A", "camp-portfolio-a/WU-A-OTHER", "camp-portfolio-b/WU-B",
    ]
    assert decision["selected_candidate_ids"] == [
        "camp-portfolio-a/WU-A", "camp-portfolio-b/WU-B",
    ]
    assert decision["selection_reason"] == "highest_score_then_stable_id"
    rows = {row["candidate_id"]: row for row in decision["considered_candidates"]}
    assert rows["camp-portfolio-a/WU-A-CHILD"]["eligible"] is False
    assert rows["camp-portfolio-a/WU-A-CHILD"]["exclusion_reasons"] == ["dependency_not_completed:WU-A"]
    assert rows["camp-portfolio-a/WU-A"]["portfolio_rank"] == 1
    assert rows["camp-portfolio-a/WU-A-OTHER"]["portfolio_rank"] == 2
    assert rows["camp-portfolio-b/WU-B"]["portfolio_rank"] == 3
    assert "camp-portfolio-a/WU-A-OTHER" not in decision["selected_candidate_ids"]

    cli = json.loads(run_campaign([
        "portfolio-schedule-preview", "--max-concurrent-wu", "2",
    ], state_home).stdout)
    assert cli == decision
    assert tree_bytes(campaigns_root) == before

    bounded = portfolio.preview_portfolio(campaigns_root, max_concurrent_wu=1)
    assert bounded["selected_candidate_ids"] == ["camp-portfolio-a/WU-A"]
    assert bounded["eligible_candidate_ids"] == decision["eligible_candidate_ids"]
    assert bounded["selection_reason"] == "highest_score_then_stable_id"

    only_b = portfolio.preview_portfolio(campaigns_root, ["camp-portfolio-b"], max_concurrent_wu=1)
    assert only_b["selected_candidate_ids"] == ["camp-portfolio-b/WU-B"]
    assert [item["campaign_id"] for item in only_b["campaigns"]] == ["camp-portfolio-b"]

    # An occupied global portfolio is a deterministic no-selection result, not
    # permission to exceed the configured concurrency ceiling.
    b_path = campaign_b / "campaign.json"
    b_data = json.loads(b_path.read_text(encoding="utf-8"))
    b_data["revision"] = 8
    b_data["work_units"][0]["status"] = "active"
    b_data["work_units"][0]["phase"] = "implementing"
    b_data["active_work_unit"] = "WU-B"
    b_path.write_text(json.dumps(b_data, indent=2) + "\n", encoding="utf-8")
    exhausted = portfolio.preview_portfolio(campaigns_root, max_concurrent_wu=1)
    assert exhausted["active_work_unit_count"] == 1
    assert exhausted["selected_candidate_ids"] == []
    assert exhausted["selection_reason"] == "portfolio_concurrency_exhausted"

    # Rehashing an altered selection cannot turn an invalid ranking into a
    # valid decision; the validator checks policy-derived order as well as ID.
    tampered = copy.deepcopy(decision)
    tampered["eligible_candidate_ids"] = list(reversed(tampered["eligible_candidate_ids"]))
    body = {key: value for key, value in tampered.items() if key not in {"decision_id", "decision_digest"}}
    tampered["decision_digest"] = portfolio._sha(body)
    tampered["decision_id"] = "portfolio_schedule_" + tampered["decision_digest"]
    try:
        portfolio.validate_portfolio_decision(tampered)
    except portfolio.PortfolioInputInvalid:
        pass
    else:
        raise AssertionError("rehashed non-policy portfolio ranking was accepted")

    invalid = run_campaign([
        "portfolio-schedule-preview", "--max-concurrent-wu", "0",
    ], state_home, ok=False)
    assert "positive" in json.loads(invalid.stdout)["error"]

print("portfolio scheduler: multi-campaign coordination, bounded capacity, deterministic replay, and read-only authority PASS")
