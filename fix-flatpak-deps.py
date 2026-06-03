#!/usr/bin/env python3
"""
fix-flatpak-deps.py — Replace PyPI source tarballs in python3-deps.json
with pre-built wheels for the target architectures.

Usage:
    python3 fix-flatpak-deps.py [--arch ARCH ...] python3-deps.json

By default, emits multi-arch entries (x86_64 + aarch64) so a single
manifest covers both Flatpak builds. Each wheel is tagged with
``only-arches`` so flatpak-builder extracts the right one per arch.

flatpak-pip-generator produces a nested module tree:
  {
    "name": "python3-deps",        <-- top-level wrapper module
    "modules": [
      {
        "name": "python3-cryptography",
        "sources": [               <-- tarballs are HERE, one level down
          { "url": "...cryptography-46.0.6.tar.gz" }
        ]
      }, ...
    ]
  }

The script walks this tree recursively, finds tarball sources from
PyPI, queries the PyPI JSON API for matching pre-built wheels (one
per arch), and replaces each tarball with N per-arch wheel entries
in-place. A wheel that's already pure-Python (``py3-none-any``) is
left alone — flatpak-pip-generator already emits those as wheels.
"""

import argparse, json, re, sys, urllib.request
from pathlib import Path

_ABI_PREFERENCE = [
    "cp311-abi3",
    "cp38-abi3",
    "cp37-abi3",
    "cp313-cp313",
    "cp313-abi3",
    "cp312-cp312",
    "cp312-abi3",
    "cp311-cp311",
]

# Flatpak-builder accepts these arch identifiers in ``only-arches``.
_DEFAULT_ARCHES = ("x86_64", "aarch64")


def _compatible_with_cp313(filename):
    """Return True if the wheel is usable by CPython 3.13."""
    stem = filename[:-4]  # remove .whl
    parts = stem.split("-")
    if len(parts) < 5:
        return False
    python_tag, abi_tag = parts[2], parts[3]
    if abi_tag == "none":
        return True
    if abi_tag == "abi3":
        m = re.match(r"cp3(\d+)$", python_tag)
        if m and int(m.group(1)) <= 13:
            return True
        return False
    if python_tag == "cp313" and abi_tag == "cp313":
        return True
    return False


def pypi_wheels_for(name, version, arch):
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as exc:
        print(f"    WARNING: could not query PyPI for {name} {version}: {exc}")
        return []
    wheels = [u for u in data.get("urls", [])
              if u["filename"].endswith(".whl")
              and arch in u["filename"]
              and "linux" in u["filename"]
              and _compatible_with_cp313(u["filename"])]
    def rank(w):
        fn = w["filename"]
        for i, tag in enumerate(_ABI_PREFERENCE):
            if tag in fn:
                return i
        return len(_ABI_PREFERENCE)
    return sorted(wheels, key=rank)


def parse_tarball(source):
    url = source.get("url", "")
    if not url.startswith("https://files.pythonhosted.org/"):
        return None
    fn = url.split("/")[-1]
    if fn.endswith(".whl"):
        return None
    m = re.match(r"^([A-Za-z0-9_.-]+?)-(\d[^-]*)\.(tar\.gz|zip)$", fn)
    if not m:
        return None
    name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
    return name, m.group(2)


def fix_sources(module, arches):
    """Replace tarball sources with per-arch wheel entries. Returns the
    number of tarballs that were replaced (one count per tarball, no
    matter how many arches it expanded into)."""
    sources = module.get("sources", [])
    new_sources = []
    replaced = 0
    for src in sources:
        result = parse_tarball(src)
        if result is None:
            new_sources.append(src)
            continue
        name, version = result
        fn = src["url"].split("/")[-1]
        print(f"    tarball : {fn}")
        per_arch = []
        for arch in arches:
            print(f"    querying: PyPI for {name} {version} {arch} wheel ...", end=" ", flush=True)
            wheels = pypi_wheels_for(name, version, arch)
            if not wheels:
                print(f"\n    WARNING : no {arch} wheel found")
                continue
            best = wheels[0]
            print("found")
            print(f"    {arch:>7} : {best['filename']}")
            per_arch.append({
                "type": "file",
                "url": best["url"],
                "sha256": best["digests"]["sha256"],
                "only-arches": [arch],
            })
        if not per_arch:
            print(f"    WARNING : no wheels found for any arch — leaving tarball unchanged")
            new_sources.append(src)
            continue
        new_sources.extend(per_arch)
        replaced += 1
    module["sources"] = new_sources
    return replaced


def walk(obj, arches, depth=0):
    total = 0
    if isinstance(obj, list):
        for item in obj:
            total += walk(item, arches, depth)
        return total
    name = obj.get("name", "<unnamed>")
    print("  " * depth + f"module: {name}")
    total += fix_sources(obj, arches)
    for sub in obj.get("modules", []):
        total += walk(sub, arches, depth + 1)
    return total


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("json_file")
    p.add_argument("--arch", action="append",
                   help="Target arch (repeatable). Defaults to x86_64 + aarch64.")
    args = p.parse_args()

    arches = tuple(args.arch) if args.arch else _DEFAULT_ARCHES

    path = Path(args.json_file)
    if not path.exists():
        sys.exit(f"Error: {path} not found")

    original = path.read_text()
    data = json.loads(original)

    print(f"Scanning {path} for tarball sources to replace ({', '.join(arches)}) ...\n")
    replaced = walk(data, arches)

    if replaced == 0:
        print("\nNo tarballs needed replacing — nothing to do.")
        return

    backup = path.with_suffix(".json.bak")
    backup.write_text(original)
    print(f"\nBackup : {backup}")
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Patched {replaced} module(s) in {path}")
    print("\nDone. Re-run flatpak-builder now.")


if __name__ == "__main__":
    main()
