#!/usr/bin/env python3
"""Build and self-test a reproducible Myrmex release archive."""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,subprocess,tempfile,zipfile
from pathlib import Path

EXCLUDED={".git",".github","dist","external-sources","__pycache__"}
def files(root):
 out=[]
 for p in root.rglob("*"):
  if not p.is_file(): continue
  rel=p.relative_to(root)
  if any(part in EXCLUDED for part in rel.parts) or p.name in {"PACKAGE-MANIFEST.json","MANIFEST.sha256"} or p.suffix in {".zip",".pyc"} or p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example"): continue
  out.append(rel)
 return sorted(out)
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("--output",default="dist"); ap.add_argument("--skip-tests",action="store_true"); ap.add_argument("--reproducibility-check",action="store_true"); a=ap.parse_args()
 root=Path(__file__).resolve().parents[1]; version=(root/"VERSION").read_text().strip()
 out=Path(a.output).resolve(); out.mkdir(parents=True,exist_ok=True)
 name=f"myrmex-orchestrator-v{version}"; archive=out/(name+".zip")
 with tempfile.TemporaryDirectory(prefix="myrmex-release-") as td:
  stage=Path(td)/name; stage.mkdir()
  for rel in files(root):
   dest=stage/rel; dest.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(root/rel,dest)
  manifest={"schema":"myrmex.package-manifest/v1","version":version,"files":[str(x).replace(os.sep,"/") for x in files(stage)]}
  (stage/"PACKAGE-MANIFEST.json").write_text(json.dumps(manifest,indent=2)+"\n")
  entries=sorted([p for p in stage.rglob("*") if p.is_file() and p.name!="MANIFEST.sha256"])
  lines=[]
  for p in entries:
   rel=p.relative_to(stage).as_posix(); lines.append(hashlib.sha256(p.read_bytes()).hexdigest()+"  "+rel)
  (stage/"MANIFEST.sha256").write_text("\n".join(lines)+"\n")
  prohibited=["in"+"mobidev","ar"+"kana","invest"+"anddream","gmail"+".com"]
  for pth in stage.rglob("*"):
   if not pth.is_file() or pth.name in {"PACKAGE-MANIFEST.json","MANIFEST.sha256"}: continue
   text=pth.read_text(encoding="utf-8",errors="ignore").lower()
   if any(token in text for token in prohibited) or any(part.startswith("/home/") and part != "/home/USER/" for part in text.split() if part.startswith("/home/")):
    raise SystemExit("release contains prohibited identity or user path: "+str(pth.relative_to(stage)))
  if not a.skip_tests:
   env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1")
   subprocess.run([str(stage/"scripts/run-tests.sh")],check=True,cwd=stage,env=env)
   cfg=Path(td)/"config"; bindir=Path(td)/"bin"
   subprocess.run([str(stage/"scripts/preflight.sh"),"--config-dir",str(cfg),"--bin-dir",str(bindir)],check=True,cwd=stage,env=env)
  with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED,compresslevel=9) as z:
   for p in sorted([x for x in stage.rglob("*") if x.is_file()]):
    info=zipfile.ZipInfo(name+"/"+p.relative_to(stage).as_posix(),(1980,1,1,0,0,0)); info.compress_type=zipfile.ZIP_DEFLATED; info.external_attr=(p.stat().st_mode & 0o777)<<16
    z.writestr(info,p.read_bytes())
  extracted=Path(td)/"extracted"; extracted.mkdir()
  with zipfile.ZipFile(archive) as z: z.extractall(extracted)
  unpacked=extracted/name
  for executable in [*unpacked.glob("scripts/*.sh"), *unpacked.glob("scripts/*.py"), *unpacked.glob("tests/*.sh"), unpacked/"bin/myrmex-state", unpacked/"bin/myrmex-memory"]:
   if executable.is_file(): executable.chmod(executable.stat().st_mode | 0o111)
  if not a.skip_tests:
   env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1")
   subprocess.run([str(unpacked/"scripts/run-tests.sh")],check=True,cwd=unpacked,env=env)
   subprocess.run([str(unpacked/"scripts/preflight.sh"),"--config-dir",str(Path(td)/"unpacked-config"),"--bin-dir",str(Path(td)/"unpacked-bin")],check=True,cwd=unpacked,env=env)
 digest=hashlib.sha256(archive.read_bytes()).hexdigest(); (archive.with_suffix(".zip.sha256")).write_text(digest+"  "+archive.name+"\n")
 result={"archive":str(archive),"sha256":digest,"version":version}
 if a.reproducibility_check:
  with tempfile.TemporaryDirectory(prefix="myrmex-repro-") as repro:
   first=Path(repro)/"a"; second=Path(repro)/"b"
   for target in (first, second):
    subprocess.run([os.environ.get("PYTHON", "python3"), str(Path(__file__).resolve()), "--output", str(target), "--skip-tests"], check=True, cwd=root)
   first_zip=next(first.glob("*.zip")); second_zip=next(second.glob("*.zip"))
   first_sha=hashlib.sha256(first_zip.read_bytes()).hexdigest()
   second_sha=hashlib.sha256(second_zip.read_bytes()).hexdigest()
   if first_sha != second_sha:
    raise SystemExit(f"non-reproducible release: {first_sha} != {second_sha}")
   result["reproducible"]=True
   result["reproducibility_sha256"]=first_sha
 print(json.dumps(result,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
