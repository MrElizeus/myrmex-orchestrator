#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_AGENTS = {
    "myrmex-orchestrator.md": "primary",
    "myrmex-scout.md": "subagent",
    "myrmex-worker.md": "subagent",
    "myrmex-verifier.md": "subagent",
    "myrmex-frontier.md": "subagent",
}
EXPECTED_AGENT_STEPS = {
    "myrmex-orchestrator.md": None,
    "myrmex-frontier.md": None,
    "myrmex-scout.md": "80",
    "myrmex-worker.md": "110",
    "myrmex-verifier.md": "90",
}
REQUIRED_SKILLS = {
    "myrmex-delegation",
    "myrmex-frontier-delegation",
    "myrmex-memory",
    "myrmex-git-delivery",
}
REQUIRED_COMMANDS = {
    "myrmex-doctor.md",
    "myrmex-frontier.md",
    "myrmex-frontier-interactive.md",
    "myrmex-direct.md",
    "myrmex-delegate.md",
    "myrmex-resume.md",
    "myrmex-status.md",
}
REQUIRED_CONTRACTS = {
    "repository-context-v1.schema.json",
    "work-order-v1.schema.json",
    "work-result-v1.schema.json",
    "verification-request-v1.schema.json",
    "verification-result-v1.schema.json",
    "frontier-exchange-v1.schema.json",
    "frontier-exchange-result-v1.schema.json",
    "frontier-state-v1.schema.json",
    "frontier-state-v2.schema.json",
    "memory-v1.schema.json",
    "work-unit-metric-v1.schema.json",
    "evidence-receipt-v1.schema.json",
}
REQUIRED_FRONTIER_REFS = {
    "state-machine.md",
    "local-and-engram-state.md",
    "repository-context.md",
    "browser-transport.md",
    "response-validation.md",
    "delegation-and-verification.md",
    "security.md",
    "recovery.md",
    "git-and-completion.md",
}
IGNORED_TREE_NAMES = {
    ".git",
    "external-sources",
    "dist",
    "build",
    "node_modules",
}


def is_ignored_path(path: Path) -> bool:
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        return False
    return any(part in IGNORED_TREE_NAMES for part in rel.parts)


def package_rglob(pattern: str) -> list[Path]:
    return [path for path in ROOT.rglob(pattern) if not is_ignored_path(path)]


def frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        raise ValueError("missing opening frontmatter delimiter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("missing closing frontmatter delimiter")
    return text[4:end]


def field_value(fm: str, field: str) -> str | None:
    prefix = field + ":"
    for line in fm.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"\'')
    return None


def run(command: list[str], *, env: dict[str, str] | None = None, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)


def require_text(path: Path, needles: list[str], errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            errors.append(f"{path.relative_to(ROOT)}: missing invariant {needle!r}")


def find_prohibited_tokens(text: str) -> list[str]:
    lower = text.lower()
    tokens = ["in" + "mobidev", "ar" + "kana", "invest" + "anddream", "/home/" + "eliseo", "gmail" + ".com"]
    return [token for token in tokens if token in lower]

def validate_markdown_links(errors: list[str]) -> None:
    link_re = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    for path in package_rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for target in link_re.findall(text):
            target = target.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            candidate = (path.parent / target).resolve()
            try:
                candidate.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"{path.relative_to(ROOT)}: link escapes package: {target}")
                continue
            if not candidate.exists():
                errors.append(f"{path.relative_to(ROOT)}: broken link: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the Myrmex package")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="skip optional schema meta-validation and functional helper tests",
    )
    args = parser.parse_args()

    errors: list[str] = []
    warnings: list[str] = []

    for filename, expected_mode in REQUIRED_AGENTS.items():
        path = ROOT / "agents" / filename
        if not path.is_file():
            errors.append(f"missing agent: {path.relative_to(ROOT)}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
            fm = frontmatter(text)
            mode = field_value(fm, "mode")
            if mode != expected_mode:
                errors.append(f"{filename}: mode={mode!r}, expected {expected_mode!r}")
            if not field_value(fm, "description"):
                errors.append(f"{filename}: missing description")
            steps = field_value(fm, "steps")
            expected_steps = EXPECTED_AGENT_STEPS[filename]
            if expected_steps is None and steps is not None:
                errors.append(
                    f"{filename}: top-level steps field must be absent for a long-lived agent, found {steps!r}"
                )
            elif expected_steps is not None and steps != expected_steps:
                errors.append(
                    f"{filename}: steps={steps!r}, expected bounded value {expected_steps!r}"
                )
            if filename != "myrmex-orchestrator.md" and expected_mode == "subagent" and field_value(fm, "hidden") != "true":
                errors.append(f"{filename}: subagent must be hidden")
            if ("/home/" + "eliseo") in text:
                errors.append(f"{filename}: contains a user-specific absolute path")
        except Exception as exc:
            errors.append(f"{filename}: {exc}")

    agent_dir = ROOT / "agents"
    require_text(agent_dir / "myrmex-orchestrator.md", [
        '"myrmex-frontier": allow',
        '"playwright_*": deny', '"git push --force*": deny', "**DIRECT** — default",
    ], errors)
    require_text(agent_dir / "myrmex-scout.md", [
        "edit: deny", "task: deny", '"mem_*": deny', '"playwright_*": deny', '"*": deny',
    ], errors)
    require_text(agent_dir / "myrmex-worker.md", [
        "task: deny", '"mem_*": deny', '"playwright_*": deny',
        '"git commit*": deny', '"git push*": deny', '"myrmex-memory*": deny', '"myrmex-git-delivery": deny',
    ], errors)
    require_text(agent_dir / "myrmex-verifier.md", [
        "edit: deny", "task: deny", '"mem_*": deny',
        '"git commit*": deny', '"git push*": deny', '"myrmex-memory*": deny', '"myrmex-git-delivery": deny',
    ], errors)
    require_text(agent_dir / "myrmex-frontier.md", [
        '"*": deny', "edit: deny", "bash: deny", "task: deny",
        '"mem_*": deny', '"playwright_*": allow', "document.body.innerText",
    ], errors)

    for skill in REQUIRED_SKILLS:
        path = ROOT / "skills" / skill / "SKILL.md"
        if not path.is_file():
            errors.append(f"missing skill: {path.relative_to(ROOT)}")
            continue
        try:
            fm = frontmatter(path.read_text(encoding="utf-8"))
            name = field_value(fm, "name")
            if name != skill:
                errors.append(f"{path.relative_to(ROOT)}: name={name!r}, expected {skill!r}")
            if not field_value(fm, "description"):
                errors.append(f"{path.relative_to(ROOT)}: missing description")
        except Exception as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")

    frontier_refs = ROOT / "skills" / "myrmex-frontier-delegation" / "references"
    for name in REQUIRED_FRONTIER_REFS:
        if not (frontier_refs / name).is_file():
            errors.append(f"missing frontier reference: {name}")

    frontier_skill = (ROOT / "skills" / "myrmex-frontier-delegation" / "SKILL.md").read_text(encoding="utf-8")
    for name in REQUIRED_FRONTIER_REFS:
        if f"references/{name}" not in frontier_skill:
            errors.append(f"frontier skill does not require reference: {name}")

    for command in REQUIRED_COMMANDS:
        path = ROOT / "commands" / command
        if not path.is_file():
            errors.append(f"missing command: {path.relative_to(ROOT)}")
            continue
        try:
            fm = frontmatter(path.read_text(encoding="utf-8"))
            if field_value(fm, "agent") != "myrmex-orchestrator":
                errors.append(f"{command}: expected agent myrmex-orchestrator")
            if not field_value(fm, "description"):
                errors.append(f"{command}: missing description")
        except Exception as exc:
            errors.append(f"{command}: {exc}")

    for name in REQUIRED_CONTRACTS:
        if not (ROOT / "contracts" / name).is_file():
            errors.append(f"missing canonical contract: contracts/{name}")

    json_files = sorted({
        *list((ROOT / "contracts").glob("*.json")),
        *list((ROOT / "skills" / "myrmex-frontier-delegation" / "assets" / "schemas").glob("*.json")),
        *list((ROOT / "skills" / "myrmex-delegation" / "assets" / "schemas").glob("*.json")),
        *list((ROOT / "skills" / "myrmex-memory" / "assets" / "schemas").glob("*.json")),
        *list((ROOT / "examples").glob("*.json")),
        *list((ROOT / "profiles").glob("*.json")),
    })
    parsed_json: dict[Path, object] = {}
    for path in json_files:
        try:
            parsed_json[path] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid JSON {path.relative_to(ROOT)}: {exc}")

    if not args.quick:
        try:
            import jsonschema  # type: ignore
        except ImportError:
            warnings.append("jsonschema unavailable; skipped schema meta-validation")
        else:
            for path, data in parsed_json.items():
                if path.name.endswith("schema.json") and isinstance(data, dict):
                    try:
                        jsonschema.Draft202012Validator.check_schema(data)
                    except Exception as exc:
                        errors.append(f"invalid JSON Schema {path.relative_to(ROOT)}: {exc}")

    canonical_copies = {
        "repository-context-v1.schema.json": [
            ROOT / "skills/myrmex-delegation/assets/schemas/repository-context-v1.schema.json",
            ROOT / "skills/myrmex-frontier-delegation/assets/schemas/repository-context-v1.schema.json",
        ],
        "work-order-v1.schema.json": [ROOT / "skills/myrmex-delegation/assets/schemas/work-order-v1.schema.json"],
        "work-result-v1.schema.json": [ROOT / "skills/myrmex-delegation/assets/schemas/work-result-v1.schema.json"],
        "verification-request-v1.schema.json": [ROOT / "skills/myrmex-delegation/assets/schemas/verification-request-v1.schema.json"],
        "verification-result-v1.schema.json": [ROOT / "skills/myrmex-delegation/assets/schemas/verification-result-v1.schema.json"],
        "frontier-exchange-v1.schema.json": [ROOT / "skills/myrmex-frontier-delegation/assets/schemas/frontier-exchange.schema.json"],
        "frontier-exchange-result-v1.schema.json": [ROOT / "skills/myrmex-frontier-delegation/assets/schemas/frontier-exchange-result.schema.json"],
        "frontier-state-v2.schema.json": [ROOT / "skills/myrmex-frontier-delegation/assets/schemas/frontier-state.schema.json"],
        "memory-v1.schema.json": [ROOT / "skills/myrmex-memory/assets/schemas/memory-v1.schema.json"],
        "work-unit-metric-v1.schema.json": [ROOT / "skills/myrmex-memory/assets/schemas/work-unit-metric-v1.schema.json"],
    }
    for canonical, copies in canonical_copies.items():
        source = ROOT / "contracts" / canonical
        if not source.is_file():
            continue
        for copy in copies:
            if not copy.is_file():
                errors.append(f"missing schema copy: {copy.relative_to(ROOT)}")
            elif source.read_bytes() != copy.read_bytes():
                errors.append(f"schema drift: {copy.relative_to(ROOT)} != contracts/{canonical}")

    js = ROOT / "skills/myrmex-frontier-delegation/assets/dom/latest-assistant-message.js"
    if not js.is_file():
        errors.append(f"missing DOM helper: {js.relative_to(ROOT)}")
    else:
        try:
            proc = run(["node", "--check", str(js)])
            if proc.returncode != 0:
                errors.append(f"invalid JavaScript {js.relative_to(ROOT)}: {proc.stderr.strip()}")
            if not args.quick:
                test = run(["node", str(ROOT / "tests/test-frontier-dom.js")])
                if test.returncode != 0:
                    errors.append(f"frontier DOM test failed: {test.stderr.strip() or test.stdout.strip()}")
        except FileNotFoundError:
            warnings.append("node unavailable; skipped JavaScript syntax/DOM tests")
        except Exception as exc:
            warnings.append(f"JavaScript tests skipped: {exc}")

    state_bin = ROOT / "bin/myrmex-state"
    if not state_bin.is_file():
        errors.append("missing bin/myrmex-state")
    elif not os.access(state_bin, os.X_OK):
        errors.append("bin/myrmex-state is not executable")
    else:
        try:
            compile(state_bin.read_text(encoding="utf-8"), str(state_bin), "exec")
            if not args.quick:
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
                state_test = run([sys.executable, str(ROOT / "tests/test-state-cli.py")], env=env)
                if state_test.returncode != 0:
                    errors.append(f"myrmex-state test failed: {state_test.stderr.strip() or state_test.stdout.strip()}")
        except SyntaxError as exc:
            errors.append(f"myrmex-state syntax error: {exc}")

    memory_bin = ROOT / "bin/myrmex-memory"
    if not memory_bin.is_file():
        errors.append("missing bin/myrmex-memory")
    elif not os.access(memory_bin, os.X_OK):
        errors.append("bin/myrmex-memory is not executable")
    else:
        try:
            compile(memory_bin.read_text(encoding="utf-8"), str(memory_bin), "exec")
            if not args.quick:
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
                memory_test = run([sys.executable, str(ROOT / "tests/test-memory-cli.py")], env=env)
                if memory_test.returncode != 0:
                    errors.append(f"myrmex-memory test failed: {memory_test.stderr.strip() or memory_test.stdout.strip()}")
        except SyntaxError as exc:
            errors.append(f"myrmex-memory syntax error: {exc}")

    for script in [*list((ROOT / "scripts").glob("*.py")), *list((ROOT / "tests").glob("*.py"))]:
        try:
            compile(script.read_text(encoding="utf-8"), str(script), "exec")
        except SyntaxError as exc:
            errors.append(f"Python syntax error {script.relative_to(ROOT)}: {exc}")
    for script in [*list((ROOT / "scripts").glob("*.sh")), *list((ROOT / "tests").glob("*.sh"))]:
        proc = run(["bash", "-n", str(script)])
        if proc.returncode != 0:
            errors.append(f"shell syntax error {script.relative_to(ROOT)}: {proc.stderr.strip()}")

    required_root = {
        "README.md", "README.es.md", "INSTALL.md", "PROMPT-INSTALL-MYRMEX.md", "PROMPT-LIVE-SMOKE-TEST.md",
        "VERSION", "LICENSE", "NOTICE", "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
        "SECURITY.md", "requirements-dev.txt", ".gitattributes",
    }
    for name in required_root:
        if not (ROOT / name).is_file():
            errors.append(f"missing root file: {name}")
    for name in ["build-release.py", "collect-git-evidence.py", "inspect-agent-resolution.py", "validate-diff-size.py", "verify-receipt.py", "myrmex-git-local.py"]:
        if not (ROOT / "scripts" / name).is_file():
            errors.append(f"missing release/control script: scripts/{name}")
    if (ROOT / ".github").exists() and not (ROOT / ".github" / "workflows" / "ci.yml").is_file():
        errors.append("missing GitHub CI workflow: .github/workflows/ci.yml")

    validate_markdown_links(errors)

    generated = [
        p.relative_to(ROOT).as_posix()
        for p in package_rglob("*")
        if p.name == "__pycache__" or p.suffix == ".pyc"
    ]
    if generated:
        errors.append("generated Python cache files present: " + ", ".join(generated))

    text_paths = [
        p for p in package_rglob("*")
        if p.is_file()
        and p.suffix in {".md", ".py", ".sh", ".json", ".js"}
        and p != Path(__file__).resolve()
    ]
    all_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in text_paths)
    stale_patterns = {
        "retired explorer identifier": "myrmex-explorer",
        "retired project name": "eigenhive",
        "retired package directory": "myrmex-orchestrator-kit",
        "external reference doc": "REFERENCE-SOURCES",
        "unbounded Playwright MCP version": "@playwright/mcp@latest",
        "private identity a": "in" + "mobidev",
        "private identity b": "ar" + "kana",
        "private identity c": "invest" + "anddream",
    }
    lower = all_text.lower()
    for label, value in stale_patterns.items():
        if value.lower() in lower:
            errors.append(f"{label} remains: {value}")
    for token in find_prohibited_tokens(all_text):
        errors.append(f"prohibited identity/path remains: {token}")
    if re.search(r"/home/(?!USER(?:/|$))[A-Za-z0-9_.-]+", all_text):
        errors.append("user-specific absolute home path remains")

    result = {
        "ok": not errors,
        "agents": len(REQUIRED_AGENTS),
        "skills": len(REQUIRED_SKILLS),
        "commands": len(REQUIRED_COMMANDS),
        "contracts": len(REQUIRED_CONTRACTS),
        "schemas_checked": sum(1 for p in json_files if p.name.endswith("schema.json")),
        "validation_mode": "quick" if args.quick else "full",
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
