#!/usr/bin/env python3
"""Collect deterministic Git evidence for a Myrmex receipt."""
from __future__ import annotations
import argparse, json, subprocess
from pathlib import Path

def git(repo: Path, *args: str) -> str:
    p=subprocess.run(["git","-C",str(repo),*args],capture_output=True,text=True,check=True)
    return p.stdout

def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--repo",required=True)
    p.add_argument("--base-sha",default="")
    args=p.parse_args()
    repo=Path(args.repo).expanduser().resolve()
    if not (repo/".git").exists(): raise SystemExit(f"not a git repository: {repo}")
    head=git(repo,"rev-parse","HEAD").strip()
    branch=git(repo,"branch","--show-current").strip()
    status=git(repo,"status","--short").splitlines()
    diff_args=["diff","--numstat"]
    if args.base_sha: diff_args.append(args.base_sha)
    numstat=git(repo,*diff_args).splitlines()
    files=[]; additions=deletions=0
    for line in numstat:
        parts=line.split("\t",2)
        if len(parts)!=3: continue
        add,delete,path=parts
        if add=="-" or delete=="-": continue
        a,d=int(add),int(delete); additions+=a; deletions+=d; files.append(path)
    check_args=["diff","--check"]
    if args.base_sha: check_args.append(args.base_sha)
    check=subprocess.run(["git","-C",str(repo),*check_args],capture_output=True,text=True)
    result={"schema":"myrmex.evidence-receipt/v1","branch":branch,"head":head,"base_sha":args.base_sha or None,"files":files,"additions":additions,"deletions":deletions,"changed_lines":additions+deletions,"status":status,"diff_check":"pass" if check.returncode==0 else "fail"}
    if check.returncode: result["diff_check_output"]=check.stdout+check.stderr
    print(json.dumps(result,indent=2,ensure_ascii=False,sort_keys=True)); return 0 if check.returncode==0 else 1
if __name__=="__main__": raise SystemExit(main())
