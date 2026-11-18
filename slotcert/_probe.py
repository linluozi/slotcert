"""Subprocess probe: measure one class before/after in an isolated interpreter."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import pickle
import platform
import statistics
import sys
import time
import tracemalloc


def _attrs(obj) -> list[str]:
    slots = getattr(type(obj), "__slots__", None)
    if slots:
        return [name for name in slots if name not in ("__weakref__", "__dict__")]
    return [name for name in vars(obj) if not name.startswith("__")]


def _median_ns(fn, repeats: int) -> float:
    gc.collect()
    gc.disable()
    try:
        fn()  # warmup outside the samples
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            fn()
            samples.append(time.perf_counter() - start)
    finally:
        gc.enable()
    return statistics.median(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("module")
    parser.add_argument("samples", help="module:attribute of a {class_name: factory} dict")
    parser.add_argument("class_name")
    parser.add_argument("--n", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=20261119)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args(argv)

    mod = importlib.import_module(args.module)
    cls = getattr(mod, args.class_name)
    samples_mod, attr = args.samples.split(":")
    factories = getattr(importlib.import_module(samples_mod), attr)
    factory = factories[args.class_name]

    for _ in range(3):
        factory()
    gc.collect()

    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    objs = [factory() for _ in range(args.n)]
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    bytes_per_instance = (current - base) / args.n

    probe = objs[0]
    names = _attrs(probe)

    m = min(max(args.n, 50_000), 200_000)
    construct = _median_ns(lambda: [factory() for _ in range(m)], args.repeats)
    passes = max(3, 2_000_000 // max(1, len(objs) * len(names)))
    access = _median_ns(
        lambda: [getattr(obj, name) for _ in range(passes) for obj in objs for name in names],
        args.repeats)

    digest = hashlib.sha256()
    roundtrip_ok = True
    for obj in objs[:20_000]:
        state = [(name, getattr(obj, name)) for name in sorted(names)]
        digest.update(pickle.dumps(state, protocol=4))
        clone = pickle.loads(pickle.dumps(obj, protocol=4))
        if [(name, getattr(clone, name)) for name in sorted(names)] != state:
            roundtrip_ok = False

    result = {
        "module": args.module,
        "class": args.class_name,
        "n": args.n,
        "seed": args.seed,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "slots": list(getattr(cls, "__slots__", ()) or ()),
        "attrs": names,
        "bytes_per_instance": round(bytes_per_instance, 3),
        "construct_ops_per_s": round(m / construct, 3),
        "access_ops_per_s": round(passes * len(objs) * len(names) / access, 3),
        "pickle_n": min(args.n, 20_000),
        "state_sha256": digest.hexdigest(),
        "pickle_roundtrip_ok": roundtrip_ok,
        "peak_bytes": peak,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
