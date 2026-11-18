# slotcert

**Find classes that can safely use `__slots__`, rewrite them with a one-command rollback, and prove the result with a per-class memory / throughput / state certificate.**

`slotcert` is for Python codebases whose object graphs are large enough that per-instance `__dict__` overhead matters. It is conservative by design: only classes whose `__init__` is a straight line of literal `self.x = ...` assignments, with `object` as the only base, are touched. Everything else is rejected with a named reason.

Upstream: none (original project, no upstream repository)

Unlike `dataclasses(slots=True)` it works on hand-written classes you do not want to rewrite. Unlike a sed-style `__slots__` edit it records a byte-exact backup, refuses to patch twice, refuses to roll back over edits, and produces a certificate with real `tracemalloc` numbers and a semantic state digest.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
```

Python >= 3.10, standard library only at runtime.

## Three commands

```bash
# 1. see what is safe and why the rest is not
.venv/bin/slotcert scan ledgerdemo/models.py

# 2. patch in place (backup + manifest under .slotcert), or roll back
.venv/bin/slotcert patch ledgerdemo --classes Money,Account
.venv/bin/slotcert rollback --state ledgerdemo/.slotcert

# 3. certify a copy: memory, construction, attribute access, state digest
.venv/bin/slotcert certify --package ledgerdemo --module ledgerdemo.models \
  --samples ledgerdemo.samples:FACTORIES --n 200000 --repeats 9 --out /tmp/cert
```

The repository ships a 12-class `ledgerdemo` package: 8 certifiable classes and 4 that exercise every rejection path (branching `__init__`, dynamic `setattr`, inheritance, existing `__slots__`).

## What the analyzer accepts

| Rule | Accepted | Rejected reason |
| --- | --- | --- |
| Base classes | none or `object` only | `inherits from X` |
| Class decorators / keywords | none | `decorated class` / `class keywords` |
| `__init__` body | literal `self.x = ...`, annotations, docstring, zero-arg `super().__init__()` | `complex __init__ statement ...` |
| Attribute writes elsewhere | none | `dynamic setattr(self, ...)` |
| `__dict__` / `vars(self)` | none | `uses __dict__` / `vars(self)` |
| Dynamic reads | none | `getattr(self, <dynamic>)` |
| Already slotted | reported, skipped | `already defines __slots__` |

The rewrite inserts `__slots__ = (..., '__weakref__')` after the class docstring, re-parses the result before writing, and fails closed on one-line classes. Keeping `__weakref__` costs 16 bytes per instance but preserves `weakref.ref(obj)` behaviour that a plain `__dict__` class had.

## Measured result (archived in `evidence/certificate.json`)

`n = 200,000` instances per class, `repeats = 9`, CPython 3.12.3, AMD EPYC 7K62 (4 vCPU), 3.7 GB RAM, idle machine. Memory is the `tracemalloc` delta per instance; throughput is the median of 9 timed runs with GC disabled during sampling, normalised to the unpatched copy.

| class | bytes/inst before | bytes/inst after | memory drop | construct | attribute access | state digest |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Account | 96.13 | 80.12 | 16.66% | 1.076x | 1.044x | equal |
| Customer | 96.13 | 80.12 | 16.66% | 1.306x | 1.014x | equal |
| Invoice | 104.13 | 88.12 | 15.37% | 1.033x | 1.045x | equal |
| LedgerEntry | 104.13 | 88.12 | 15.37% | 1.047x | 1.062x | equal |
| Money | 88.13 | 72.12 | 18.17% | 1.105x | 1.081x | equal |
| Payment | 104.13 | 88.12 | 15.37% | 1.049x | 1.036x | equal |
| ReportRow | 96.13 | 80.12 | 16.66% | 1.076x | 1.067x | equal |
| Transaction | 104.13 | 88.12 | 15.37% | 1.052x | 0.963x | equal |

- Median memory drop: **16.66%** (range 15.37–18.17%).
- Median construction ratio: **1.056x**; median attribute-access ratio: **1.047x**.
- Construction ratio 1.306x for `Customer` is above the rest of the cluster and should be read as timing spread, not a class-specific property. Attribute access 0.963x for `Transaction` is likewise within the spread; the drop comes from the mandatory `__weakref__` slot, which costs 16 bytes on every instance.
- **Noise disclosure:** an earlier run taken while three background processes were using 2 CPU cores produced per-class throughput ratios between 0.735x and 2.242x on the same code. The archived certificate is a rerun on an idle machine; per-class throughput is reported as-is, and the `passed` flag requires every class to stay above 0.95x. Readers comparing machines should rerun the command rather than trust the table.

State digest: for 20,000 instances per class, the tool hashes the pickled `(attribute, value)` pairs and unpickles every instance back to compare attributes. All 8 classes match before vs. after, so `__slots__` did not change observable state. Raw pickle bytes are *not* compared because a slotted class legitimately pickles a different layout; comparing bytes would be a fake equivalence test.

## Rollback semantics

`patch` copies each touched file to `<state>/backup/<relative path>` and writes `<state>/manifest.json` with `sha256_before` / `sha256_after`.

- a second `patch` against the same state directory is refused;
- `rollback` refuses to overwrite a file whose current hash is not `sha256_after`;
- after restore, every file is re-hashed and checked against `sha256_before`.

The test suite asserts byte-exact restoration of `models.py`.

## Tests

```bash
.venv/bin/python -m pytest tests -o addopts=""
# 22 passed in 4.58s
```

Coverage: all 8 safe classes, 4 rejection paths, 5 additional syntax edge cases, docstring placement, diff generation, byte-exact patch/rollback, double-patch refusal, rollback-over-edit refusal, probe determinism, end-to-end certification, CLI scan/patch/rollback and exit codes.

## Limits

- Only top-level classes with `object` (or no) base. Subclasses are reported, never patched.
- `__init__` must be straight-line; any branch, loop, `try`, or non-`self` target rejects the class.
- Class-level mutable attributes are not analysed; they do not become slots but can still shadow behaviour.
- The state digest compares the declared instance attributes only; it is not a general object-graph equality oracle.
- Adding `__weakref__` is deliberate. If you know no weak references are used, dropping it saves another 16 bytes per instance, but `slotcert` will not make that assumption for you.

## 本轮创新

**B 档：把 `__slots__` 从"人工建议"变成"可回滚、可判定的改动"。** 既有的做法要么只测量并给建议，要么手工在源码里加一行 `__slots__`；本工具首次把保守安全判定、源码改写、字节级备份/回滚、200k 实例的内存证书、构造/访问双轴吞吐、以及 20k 实例的语义状态摘要串成一个闭环，并公开每条拒绝理由与原始证书。它与"再写一个 slotted dataclass"或"再跑一次内存基准"不同：结论必须同时满足内存下降、吞吐不退化、状态摘要一致，否则 CLI 以非零码失败。

## License

MIT, Copyright (c) 2026 linluozi. Original work; no upstream code is vendored.