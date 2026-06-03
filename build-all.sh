#!/usr/bin/env bash
# build-all.sh — build Vitals flatpak bundles for x86_64 and aarch64.
#
# Usage:
#   ./build-all.sh                  # build both arches, write bundles
#   ./build-all.sh --arch x86_64    # build only one arch
#   ./build-all.sh --regen-deps     # regenerate python3-deps.json from requirements.txt
#   ./build-all.sh --install        # also install host-arch bundle (--user)
#
# Outputs:
#   vitals-x86_64.flatpak
#   vitals-aarch64.flatpak

set -euo pipefail

cd "$(dirname "$0")"

ARCHES=(x86_64 aarch64)
INSTALL=false
REGEN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install)    INSTALL=true; shift ;;
        --regen-deps) REGEN=true;   shift ;;
        --arch)       ARCHES=("$2"); shift 2 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

DEPS=build-aux/flatpak/python3-deps.json

# Vitals declares no pip dependencies (gi/GTK/Adw come from the runtime), so
# the python3-deps generation + patch below is skipped unless requirements.txt
# actually lists packages. The Flatpak manifest omits the python3-deps module.
if grep -qE '^[[:space:]]*[^#[:space:]]' requirements.txt 2>/dev/null; then

# ── Regenerate deps if requested or missing ───────────────────────────────
if $REGEN || [[ ! -f "$DEPS" ]]; then
    if ! $REGEN; then
        echo "Note: $DEPS not found — auto-regenerating from requirements.txt." >&2
    fi
    if [[ -x ~/.local/bin/flatpak_pip_generator ]]; then
        GEN=~/.local/bin/flatpak_pip_generator
    elif command -v flatpak-pip-generator >/dev/null 2>&1; then
        GEN=flatpak-pip-generator
    else
        echo "Error: flatpak_pip_generator not found on PATH or in ~/.local/bin" >&2
        exit 1
    fi
    # flatpak_pip_generator throws an ImportError during cleanup after a
    # successful save (known upstream quirk on Python 3.13+). Swallow a
    # non-zero exit if the JSON file was actually produced.
    set +e
    "$GEN" --runtime='org.gnome.Sdk//50' \
           --requirements-file=requirements.txt \
           --output build-aux/flatpak/python3-deps
    gen_status=$?
    set -e

    if [[ ! -f "$DEPS" ]]; then
        echo "Error: flatpak_pip_generator did not produce $DEPS (exit $gen_status)" >&2
        exit 1
    fi
    if (( gen_status != 0 )); then
        echo "Note: flatpak_pip_generator exited $gen_status after saving the file; continuing." >&2
    fi
fi

# ── Patch deps to use pre-built wheels (idempotent) ───────────────────────
python3 fix-flatpak-deps.py "$DEPS"

fi  # end: requirements.txt lists packages

# ── qemu-binfmt sanity check for cross-arch builds ────────────────────────
HOST_ARCH=$(uname -m)
needs_qemu=false
for a in "${ARCHES[@]}"; do
    [[ "$a" != "$HOST_ARCH" ]] && needs_qemu=true
done
if $needs_qemu; then
    if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 \
       && ! -e /proc/sys/fs/binfmt_misc/qemu-arm     ]]; then
        echo
        echo "Warning: cross-arch build requested but qemu binfmt is not registered."
        echo "         The aarch64 build will likely fail. Register binfmt with:"
        echo "             sudo systemctl restart systemd-binfmt"
        echo "         or:  sudo update-binfmts --enable qemu-aarch64"
        echo
    fi
fi

mkdir -p repo

# ── Build + bundle each arch ──────────────────────────────────────────────
for arch in "${ARCHES[@]}"; do
    builddir="_flatpak_${arch}"
    bundle="vitals-${arch}.flatpak"
    echo
    echo "==== Building Vitals for ${arch} ===="
    flatpak-builder --arch="$arch" --repo=repo --force-clean \
        "$builddir" build-aux/flatpak/land.rob.vitals.json
    echo "==== Bundling ${bundle} ===="
    flatpak build-bundle --arch="$arch" repo "$bundle" land.rob.vitals
    ls -lh "$bundle"
done

# ── Optional: install the host-arch bundle ────────────────────────────────
if $INSTALL; then
    bundle="vitals-${HOST_ARCH}.flatpak"
    if [[ -f "$bundle" ]]; then
        echo
        echo "==== Installing $bundle ===="
        flatpak install --user --noninteractive --reinstall \
            --bundle "$bundle"
    else
        echo "Note: no $bundle to install (host arch not in build set)."
    fi
fi

echo
echo "Done."
