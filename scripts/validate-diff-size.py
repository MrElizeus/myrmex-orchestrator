#!/usr/bin/env python3
"""Apply Myrmex's soft 400 changed-line limit."""
from __future__ import annotations
import argparse,json,sys
def main():
 p=argparse.ArgumentParser(); p.add_argument("--changed-lines",type=int,required=True); p.add_argument("--exception-json")
 a=p.parse_args()
 if a.changed_lines<0: raise SystemExit("changed-lines must be non-negative")
 exception=None
 if a.exception_json:
  exception=json.loads(a.exception_json)
 required=("reason","cohesion","review_strategy")
 if a.changed_lines<=400: result={"ok":True,"status":"normal","changed_lines":a.changed_lines}
 elif isinstance(exception,dict) and all(isinstance(exception.get(k),str) and exception[k].strip() for k in required):
  result={"ok":True,"status":"size:exception","changed_lines":a.changed_lines}
 else:
  result={"ok":False,"status":"FAIL_SIZE_LIMIT","changed_lines":a.changed_lines,"required_exception_fields":list(required)}
 print(json.dumps(result,indent=2))
 return 0 if result["ok"] else 1
if __name__=="__main__": raise SystemExit(main())
