#!/usr/bin/env python3
"""Fail when a declared receipt disagrees with deterministic Git evidence."""
from __future__ import annotations
import argparse,json,subprocess,sys
from pathlib import Path

def main():
 p=argparse.ArgumentParser(); p.add_argument("--repo",required=True); p.add_argument("--base-sha",default=""); p.add_argument("--receipt",required=True); a=p.parse_args()
 collector=Path(__file__).with_name("collect-git-evidence.py")
 actual=json.loads(subprocess.check_output([sys.executable,str(collector),"--repo",a.repo,"--base-sha",a.base_sha],text=True))
 candidate=Path(a.receipt)
 declared=json.loads(candidate.read_text()) if len(a.receipt) < 4096 and not a.receipt.lstrip().startswith("{") and candidate.is_file() else json.loads(a.receipt)
 keys=["branch","head","base_sha","files","additions","deletions","changed_lines","status","diff_check"]
 mismatches={k:{"declared":declared.get(k),"observed":actual.get(k)} for k in keys if declared.get(k)!=actual.get(k)}
 result={"ok":not mismatches,"error":None if not mismatches else "FAIL_RECEIPT_MISMATCH","mismatches":mismatches,"observed":actual}
 print(json.dumps(result,indent=2,ensure_ascii=False,sort_keys=True)); return 0 if not mismatches else 1
if __name__=="__main__": raise SystemExit(main())
