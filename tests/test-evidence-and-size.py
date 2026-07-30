#!/usr/bin/env python3
import json, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).parents[1]
def run(*args, ok=True):
    p=subprocess.run(args,capture_output=True,text=True)
    if ok and p.returncode: raise AssertionError(p.stderr or p.stdout)
    if not ok and p.returncode==0: raise AssertionError("unexpected success")
    return p
with tempfile.TemporaryDirectory(prefix="myrmex-evidence-") as td:
    repo=Path(td)
    subprocess.run(["git","init","-q",str(repo)],check=True)
    subprocess.run(["git","-C",str(repo),"config","user.email","test@example.invalid"],check=True)
    subprocess.run(["git","-C",str(repo),"config","user.name","Test"],check=True)
    (repo/"file.txt").write_text("one\n")
    subprocess.run(["git","-C",str(repo),"add","file.txt"],check=True)
    subprocess.run(["git","-C",str(repo),"commit","-qm","base"],check=True)
    base=subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()
    (repo/"file.txt").write_text("one\ntwo\n")
    receipt=json.loads(run("python3",str(ROOT/"scripts/collect-git-evidence.py"),"--repo",str(repo),"--base-sha",base).stdout)
    assert receipt["additions"]==1 and receipt["changed_lines"]==1
    (repo/"new-file.py").write_text("".join(f"line-{i}\n" for i in range(1000)))
    (repo/"binary.dat").write_bytes(b"\x00\x01")
    (repo/"link.txt").symlink_to("file.txt")
    receipt=json.loads(run("python3",str(ROOT/"scripts/collect-git-evidence.py"),"--repo",str(repo),"--base-sha",base).stdout)
    assert "new-file.py" in receipt["files"]
    assert receipt["untracked_files"][0]["kind"] in {"binary","symlink","text"}
    assert next(item for item in receipt["untracked_files"] if item["path"]=="new-file.py")["lines"] == 1000
    assert receipt["changed_lines"] == 1001
    oversized=run("python3",str(ROOT/"scripts/validate-diff-size.py"),"--changed-lines",str(receipt["changed_lines"]),ok=False)
    assert "FAIL_SIZE_LIMIT" in oversized.stdout
    good=run("python3",str(ROOT/"scripts/verify-receipt.py"),"--repo",str(repo),"--base-sha",base,"--receipt",json.dumps(receipt))
    assert json.loads(good.stdout)["ok"] is True
    receipt["changed_lines"]=999
    bad=run("python3",str(ROOT/"scripts/verify-receipt.py"),"--repo",str(repo),"--base-sha",base,"--receipt",json.dumps(receipt),ok=False)
    assert "FAIL_RECEIPT_MISMATCH" in bad.stdout
for value in ("399","401"):
    p=run("python3",str(ROOT/"scripts/validate-diff-size.py"),"--changed-lines",value,ok=value=="399")
    if value=="399": assert json.loads(p.stdout)["status"]=="normal"
exception='{"reason":"cohesive","cohesion":"contracts","review_strategy":"independent verifier"}'
p=run("python3",str(ROOT/"scripts/validate-diff-size.py"),"--changed-lines","401","--exception-json",exception)
assert json.loads(p.stdout)["status"]=="size:exception"
print("evidence and size policy tests: PASS")
