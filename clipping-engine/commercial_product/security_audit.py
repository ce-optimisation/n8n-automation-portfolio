"""Repeatable static security audit for the Clipping Engine source tree.

AST checks (no code is executed):
  * subprocess / os calls using a shell or string commands (command injection)
  * eval / exec / pickle / marshal / yaml.load (code execution)
  * SQL built with f-strings or % / + formatting (SQL injection review)
  * HTTP client calls without an explicit timeout (resource exhaustion)
  * ZipFile.extractall / tarfile.extractall (path traversal)
  * hard-coded binds to 0.0.0.0 (local network exposure)
  * FastAPI state-changing routes (inventory for CSRF/authorisation review)

Findings are classified; ``review`` items need human judgement, ``defect`` items
should be fixed. Output is JSON so CI or the ops script can diff it.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

_HTTP = {"get", "post", "put", "patch", "delete", "head", "request", "stream"}
_HTTP_MODULES = {"httpx", "requests", "urllib.request"}


def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _name(node.func) + "()"
    return ""


def _kw(call: ast.Call, key: str):
    return next((k.value for k in call.keywords if k.arg == key), None)


def _is_dynamic_str(node) -> bool:
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add))
                                               and any(isinstance(x, (ast.Constant, ast.JoinedStr)) and
                                                       isinstance(getattr(x, "value", ""), str) or isinstance(x, ast.JoinedStr)
                                                       for x in (node.left, node.right)))


def scan_file(path: Path, rel: str) -> tuple[list[dict], list[dict]]:
    findings, routes = [], []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [{"file": rel, "line": exc.lineno, "rule": "syntax", "severity": "defect", "detail": str(exc)}], []

    def add(node, rule, severity, detail):
        findings.append({"file": rel, "line": node.lineno, "rule": rule, "severity": severity, "detail": detail})

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and \
                        dec.func.attr in {"post", "put", "patch", "delete"} and dec.args and \
                        isinstance(dec.args[0], ast.Constant):
                    routes.append({"file": rel, "line": node.lineno, "method": dec.func.attr.upper(),
                                   "path": dec.args[0].value, "handler": node.name})
        if not isinstance(node, ast.Call):
            continue
        fn = _name(node.func)
        tail = fn.rsplit(".", 1)[-1]
        if fn.startswith("subprocess.") or tail in {"Popen", "run", "check_output", "check_call", "call"} and "subprocess" in fn:
            shell = _kw(node, "shell")
            if isinstance(shell, ast.Constant) and shell.value is True:
                add(node, "shell_true", "defect", f"{fn} with shell=True")
            if node.args and _is_dynamic_str(node.args[0]):
                add(node, "string_command", "defect", f"{fn} given a formatted string command")
        if fn in {"os.system", "os.popen"}:
            add(node, "os_shell", "defect", fn)
        if fn in {"eval", "exec"}:
            add(node, "dynamic_code", "defect", fn)
        if fn in {"pickle.load", "pickle.loads", "marshal.loads", "marshal.load"}:
            add(node, "unsafe_deserialise", "review", fn)
        if fn == "yaml.load" and not _kw(node, "Loader"):
            add(node, "yaml_load", "defect", "yaml.load without SafeLoader")
        if tail in {"execute", "executemany", "executescript", "query", "one"} and node.args and _is_dynamic_str(node.args[0]):
            add(node, "dynamic_sql", "review", f"{fn} with formatted SQL; confirm only trusted identifiers are interpolated")
        if tail in _HTTP and fn.split(".")[0] in {"httpx", "requests"} and _kw(node, "timeout") is None:
            add(node, "no_timeout", "review", f"{fn} without explicit timeout")
        if fn == "urllib.request.urlopen" and _kw(node, "timeout") is None and len(node.args) < 3:
            add(node, "no_timeout", "review", "urlopen without timeout")
        if tail == "extractall":
            add(node, "archive_extractall", "review", f"{fn}: confirm member paths are validated")
        for arg in [*node.args, *(k.value for k in node.keywords)]:
            if isinstance(arg, ast.Constant) and arg.value == "0.0.0.0":
                add(node, "wildcard_bind", "review", f"{fn} binds 0.0.0.0")
    return findings, routes


def audit(root: Path, packages: list[str]) -> dict:
    findings, routes = [], []
    for pkg in packages:
        for path in sorted((root / pkg).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if "/test" in "/" + rel or rel.split("/")[-1].startswith("test_"):
                continue
            f, r = scan_file(path, rel)
            findings += f
            routes += r
    summary: dict[str, int] = {}
    for f in findings:
        summary[f"{f['severity']}:{f['rule']}"] = summary.get(f"{f['severity']}:{f['rule']}", 0) + 1
    return {"summary": summary, "defects": [f for f in findings if f["severity"] == "defect"],
            "review": [f for f in findings if f["severity"] == "review"], "state_changing_routes": routes}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    cli.add_argument("--out", type=Path)
    a = cli.parse_args()
    report = audit(a.root, ["clipengine", "commercial_intelligence", "commercial_agency", "commercial_product"])
    if a.out:
        a.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "defects": report["defects"],
                      "routes": len(report["state_changing_routes"])}, indent=2))
    return 1 if report["defects"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
