#!/usr/bin/env python3
import json, os, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).parents[1]
agent='''---
description: test worker
mode: subagent
hidden: true
steps: 110
model: openai/test-model
---
test
'''
with tempfile.TemporaryDirectory(prefix="myrmex-resolution-") as td:
    root=Path(td); config=root/"config"; local=root/"workspace"/".opencode"/"agents"; global_dir=config/"agents"
    local.mkdir(parents=True); global_dir.mkdir(parents=True)
    (global_dir/"myrmex-worker.md").write_text(agent)
    (local/"myrmex-worker.md").write_text(agent.replace("openai/test-model","other/test-model"))
    p=subprocess.run(["python3",str(ROOT/"scripts/inspect-agent-resolution.py"),"--workspace",str(root/"workspace"),"--config-dir",str(config)],capture_output=True,text=True,check=True)
    data=json.loads(p.stdout); row=next(x for x in data["agents"] if x["agent"]=="myrmex-worker")
    assert row["shadowed"] is True and row["status"]=="BLOCKED_NON_ALLOWED_PROVIDER"
    blocked=subprocess.run(["python3",str(ROOT/"scripts/inspect-agent-resolution.py"),"--workspace",str(root/"workspace"),"--config-dir",str(config),"--enforce"],capture_output=True,text=True)
    assert blocked.returncode != 0 and "BLOCKED_NON_ALLOWED_PROVIDER" in blocked.stdout
print("agent resolution and shadowing test: PASS")
