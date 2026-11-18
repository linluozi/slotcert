from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slotcert import core  # noqa: E402
from slotcert.cert import certify  # noqa: E402
from slotcert.cli import main  # noqa: E402

MODELS = ROOT / "ledgerdemo" / "models.py"
SOURCE = MODELS.read_text(encoding="utf-8")
SAFE = ["Money", "Account", "Transaction", "LedgerEntry", "Customer", "Invoice", "Payment", "ReportRow"]


def reports():
    return {r.name: r for r in core.analyze_source(SOURCE)}


def test_all_eight_domain_classes_are_safe():
    found = reports()
    for name in SAFE:
        assert found[name].safe, found[name].reasons
        assert found[name].slots


def test_slot_order_follows_assignment_order():
    report = reports()["Transaction"]
    assert report.slots == ("txid", "account_id", "amount", "memo")


def test_conditional_init_is_rejected():
    report = reports()["ConditionalAccount"]
    assert not report.safe
    assert any("If" in reason for reason in report.reasons)


def test_dynamic_setattr_is_rejected():
    report = reports()["DynamicTag"]
    assert not report.safe
    assert any("setattr" in reason for reason in report.reasons)


def test_inheritance_is_rejected():
    report = reports()["ChildAccount"]
    assert not report.safe
    assert any("inherits" in reason for reason in report.reasons)


def test_existing_slots_is_reported_not_rewritten():
    report = reports()["AlreadySlotted"]
    assert report.already_slotted
    assert not report.safe
    outcome = core.rewrite_source(SOURCE, {"AlreadySlotted"})
    assert outcome.classes == []


@pytest.mark.parametrize("snippet,needle", [
    ("@dataclass\nclass A:\n    def __init__(self):\n        self.x = 1\n", "decorated"),
    ("class A(object):\n    def __init__(self):\n        self.x = 1\n", None),
    ("class A:\n    def __init__(self):\n        self.x = 1\n    def __getattr__(self, n):\n        return n\n", "__getattr__"),
    ("class A:\n    def __init__(self):\n        self.__dict__['x'] = 1\n", "__dict__"),
    ("class A:\n    def __init__(self):\n        self.x = 1\n        del self.x\n", "complex __init__"),
])
def test_rejection_paths(snippet, needle):
    report = core.analyze_source(snippet)[0]
    if needle is None:
        assert report.safe
    else:
        assert not report.safe
        assert any(needle in reason for reason in report.reasons)


def test_docstring_stays_first_and_slots_follow_it():
    source = 'class A:\n    """doc."""\n\n    def __init__(self):\n        self.x = 1\n'
    outcome = core.rewrite_source(source)
    assert outcome.classes == ["A"]
    lines = outcome.new_source.splitlines()
    assert lines[1].strip() == '"""doc."""'
    assert lines[3].strip() == "__slots__ = ('x', '__weakref__')"


def test_rewritten_source_parses_and_diff_is_unified():
    outcome = core.rewrite_source(SOURCE)
    assert len(outcome.classes) == len(SAFE)
    compile(outcome.new_source, "models.py", "exec")
    assert outcome.diff.count("+    __slots__") == len(SAFE)


def test_annotation_only_assignment_is_supported():
    source = "class A:\n    def __init__(self):\n        self.x: int = 1\n"
    assert core.analyze_source(source)[0].safe


def test_patch_and_rollback_are_byte_exact(tmp_path):
    target = tmp_path / "pkg"
    shutil.copytree(ROOT / "ledgerdemo", target, ignore=shutil.ignore_patterns("__pycache__"))
    before = (target / "models.py").read_bytes()
    manifest = core.patch_tree(target, {"Money", "Account"}, tmp_path / "state")
    patched = (target / "models.py").read_text()
    assert "'amount', 'currency'" in patched and "'account_id', 'owner', 'balance'" in patched
    assert len(manifest["files"]) == 1
    assert (tmp_path / "state" / "backup" / "models.py").exists()
    result = core.rollback(tmp_path / "state")
    assert result["restored"] == ["models.py"]
    assert (target / "models.py").read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == manifest["files"][0]["sha256_before"]


def test_double_patch_is_refused(tmp_path):
    target = tmp_path / "pkg"
    shutil.copytree(ROOT / "ledgerdemo", target)
    core.patch_tree(target, {"Money"}, tmp_path / "state")
    with pytest.raises(RuntimeError, match="already exists"):
        core.patch_tree(target, {"Account"}, tmp_path / "state")


def test_rollback_refuses_edited_after_patch(tmp_path):
    target = tmp_path / "pkg"
    shutil.copytree(ROOT / "ledgerdemo", target)
    core.patch_tree(target, {"Money"}, tmp_path / "state")
    (target / "models.py").write_text((target / "models.py").read_text() + "\n# edited\n")
    with pytest.raises(RuntimeError, match="refusing"):
        core.rollback(tmp_path / "state")


def test_probe_is_deterministic():
    cmd = [sys.executable, "-m", "slotcert._probe", "ledgerdemo.models",
           "ledgerdemo.samples:FACTORIES", "Money", "--n", "2000", "--repeats", "2"]
    env = {"PYTHONPATH": f"{ROOT}", "PATH": "/usr/bin:/bin"}
    a = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    b = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    ja, jb = json.loads(a.stdout), json.loads(b.stdout)
    assert ja["state_sha256"] == jb["state_sha256"]
    assert ja["pickle_roundtrip_ok"] is True


def test_certify_reports_real_memory_reduction(tmp_path):
    cert = certify(ROOT / "ledgerdemo", "ledgerdemo.models", "ledgerdemo.samples:FACTORIES",
                   ["Money", "Account"], n=5000, seed=7, out=tmp_path / "cert", repeats=3)
    assert cert["passed"], cert["gates"]
    assert len(cert["entries"]) == 2
    for entry in cert["entries"]:
        assert entry["memory_reduction_pct"] > 0
        assert entry["state_equal"] and entry["pickle_roundtrip"]
    assert json.loads((tmp_path / "cert" / "certificate.json").read_text())["passed"] is True


def test_certify_rejects_unsafe_class_request(tmp_path):
    with pytest.raises(RuntimeError, match="not safe"):
        certify(ROOT / "ledgerdemo", "ledgerdemo.models", "ledgerdemo.samples:FACTORIES",
                ["ConditionalAccount"], n=1000, seed=1, out=tmp_path / "cert", repeats=2)


def test_cli_scan_json_and_patch(tmp_path, capsys):
    assert main(["scan", str(MODELS), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert sum(1 for r in payload if r["safe"]) == 8
    target = tmp_path / "pkg"
    shutil.copytree(ROOT / "ledgerdemo", target)
    assert main(["patch", str(target), "--classes", "Money", "--state", str(tmp_path / "s")]) == 0
    assert "__slots__" in (target / "models.py").read_text()
    assert main(["rollback", "--state", str(tmp_path / "s")]) == 0
    rolled = (target / "models.py").read_text()
    assert "'amount', 'currency'" not in rolled


def test_cli_certify_exit_code(tmp_path):
    code = main(["certify", "--package", str(ROOT / "ledgerdemo"),
                 "--module", "ledgerdemo.models", "--samples", "ledgerdemo.samples:FACTORIES",
                 "--classes", "Money", "--n", "2000", "--repeats", "2",
                 "--out", str(tmp_path / "c")])
    assert code == 0