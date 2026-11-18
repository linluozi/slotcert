"""Certification: patch a copy, then measure memory, construction and access
throughput plus pickle bytes in isolated interpreters."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import patch_tree

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_probe(root: Path, module: str, samples: str, class_name: str,
              n: int, seed: int, repeats: int = 5) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(REPO_ROOT), env.get("PYTHONPATH", "")])
    cmd = [
        sys.executable, "-m", "slotcert._probe", module, samples, class_name,
        "--n", str(n), "--seed", str(seed), "--repeats", str(repeats),
    ]
    proc = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"probe failed for {class_name}: {proc.stderr.strip()[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _copy(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".slotcert"))


def certify(package: Path, module: str, samples: str, classes: list[str] | None,
            n: int, seed: int, out: Path, repeats: int = 5) -> dict:
    package = Path(package).resolve()
    out = Path(out).resolve()
    before = out / "before"
    after = out / "after"
    out.mkdir(parents=True, exist_ok=True)
    _copy(package, before / package.name)
    _copy(package, after / package.name)

    manifest = patch_tree(after, set(classes) if classes else None, after / ".slotcert")
    patched = sorted({name for entry in manifest["files"] for name in entry["classes"]})
    if classes:
        missing = sorted(set(classes) - set(patched))
        if missing:
            raise RuntimeError("classes were not safe to patch: " + ", ".join(missing))

    entries = []
    for name in patched:
        a = run_probe(before, module, samples, name, n, seed, repeats)
        b = run_probe(after, module, samples, name, n, seed, repeats)
        mem = b["bytes_per_instance"] / a["bytes_per_instance"]
        entries.append({
            "class": name,
            "slots": b.get("slots") or [],
            "bytes_before": a["bytes_per_instance"],
            "bytes_after": b["bytes_per_instance"],
            "memory_ratio": round(mem, 4),
            "memory_reduction_pct": round((1 - mem) * 100, 2),
            "construct_ops_before": a["construct_ops_per_s"],
            "construct_ops_after": b["construct_ops_per_s"],
            "construct_ratio": round(b["construct_ops_per_s"] / a["construct_ops_per_s"], 4),
            "access_ops_before": a["access_ops_per_s"],
            "access_ops_after": b["access_ops_per_s"],
            "access_ratio": round(b["access_ops_per_s"] / a["access_ops_per_s"], 4),
            "state_n": a["pickle_n"],
            "state_equal": a["state_sha256"] == b["state_sha256"],
            "pickle_roundtrip": a["pickle_roundtrip_ok"] and b["pickle_roundtrip_ok"],
        })

    gates = {
        "memory_reduced": all(e["memory_reduction_pct"] > 0 for e in entries),
        "construct_not_regressed": all(e["construct_ratio"] >= 0.95 for e in entries),
        "access_not_regressed": all(e["access_ratio"] >= 0.95 for e in entries),
        "state_identical": all(e["state_equal"] for e in entries),
        "pickle_roundtrip": all(e["pickle_roundtrip"] for e in entries),
    }
    cert = {
        "tool": "slotcert",
        "version": "0.1.0",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "package": str(package),
        "module": module,
        "samples": samples,
        "n": n,
        "seed": seed,
        "repeats": repeats,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "patched_classes": patched,
        "files": manifest["files"],
        "entries": entries,
        "gates": gates,
        "passed": all(gates.values()) and bool(entries),
    }
    (out / "certificate.json").write_text(json.dumps(cert, indent=2) + "\n", encoding="utf-8")
    return cert
