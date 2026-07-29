#!/usr/bin/env python3
"""Inspect OpenCode agent precedence, models, providers, and step limits."""
from __future__ import annotations
import argparse,json,os,re,sys
from pathlib import Path

AGENTS={"myrmex-orchestrator":None,"myrmex-scout":80,"myrmex-worker":110,"myrmex-verifier":90,"myrmex-frontier":None}
def fm(path):
    text=path.read_text(encoding="utf-8")
    if not text.startswith("---\n"): return {}
    end=text.find("\n---\n",4)
    if end<0:return {}
    out={}
    for line in text[4:end].splitlines():
        if ":" in line:
            k,v=line.split(":",1); out[k.strip()]=v.strip().strip("\"'")
    return out
def cfg(path):
    if not path.is_file(): return {}
    text=path.read_text(encoding="utf-8")
    text=re.sub(r"//.*$","",text,flags=re.M)
    text=re.sub(r",\s*([}\]])",r"\1",text)
    try: data=json.loads(text); return data if isinstance(data,dict) else {}
    except Exception: return {}
def model_from(data,name):
    value=data.get("model")
    if isinstance(value,str): return value
    agent=data.get("agent")
    if isinstance(agent,dict) and isinstance(agent.get(name),dict) and isinstance(agent[name].get("model"),str): return agent[name]["model"]
    return None
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--workspace",default=".")
    p.add_argument("--config-dir",default=os.environ.get("OPENCODE_CONFIG_DIR","~/.config/opencode"))
    p.add_argument("--policy",default=None)
    p.add_argument("--enforce",action="store_true")
    a=p.parse_args()
    workspace=Path(a.workspace).expanduser().resolve()
    config=Path(a.config_dir).expanduser().resolve()
    local=workspace/".opencode"/"agents"
    global_dir=config/"agents"
    policy={"allowed_provider_prefixes":["openai/"],"require_resolved_model_for_delegation":True,"block_shadowed_agents":True}
    policy_path=Path(a.policy).expanduser() if a.policy else workspace/"profiles"/"myrmex-defaults.json"
    if policy_path.is_file():
        data=cfg(policy_path); policy.update(data.get("agent_policy",{}))
    rows=[]; failures=[]; warnings=[]
    for name,steps in AGENTS.items():
        lp=local/(name+".md"); gp=global_dir/(name+".md")
        effective=lp if lp.is_file() else gp if gp.is_file() else None
        shadowed=lp.is_file() and gp.is_file()
        data=cfg(config/"opencode.json")
        data.update(cfg(config/"opencode.jsonc"))
        model=model_from(fm(effective),name) if effective else model_from(data,name)
        provider=model.split("/",1)[0] if model and "/" in model else None
        status="PASS_AGENT_RESOLUTION"
        if shadowed:
            warnings.append("WARN_SHADOWED_AGENT:"+name)
            status="WARN_SHADOWED_AGENT"
            if policy.get("block_shadowed_agents") and a.enforce: failures.append("WARN_SHADOWED_AGENT:"+name)
        if effective is None:
            status="FAIL_AGENT_NOT_INSTALLED"
            if a.enforce: failures.append(status+":"+name)
        expected=steps
        actual=fm(effective).get("steps") if effective else None
        if effective is not None and expected is not None and actual != str(expected):
            status="FAIL_INVALID_AGENT_STEPS"
            if a.enforce: failures.append(status+":"+name)
        if name not in {"myrmex-orchestrator","myrmex-frontier"} and policy.get("require_resolved_model_for_delegation") and not model:
            status="BLOCKED_UNRESOLVED_AGENT_MODEL"
            if a.enforce: failures.append(status+":"+name)
        if model and not any(model.startswith(prefix) for prefix in policy.get("allowed_provider_prefixes",[])):
            status="BLOCKED_NON_ALLOWED_PROVIDER"
            if a.enforce: failures.append(status+":"+name)
        rows.append({"agent":name,"effective_source":str(effective) if effective else None,"global_source":str(gp) if gp.is_file() else None,"shadowed":shadowed,"model":model,"provider":provider,"steps":int(actual) if actual and actual.isdigit() else actual,"status":status})
    result={"ok":not failures,"policy":policy,"agents":rows,"warnings":warnings,"errors":failures}
    print(json.dumps(result,indent=2,ensure_ascii=False,sort_keys=True))
    return 0 if not failures else 1
if __name__=="__main__": raise SystemExit(main())
