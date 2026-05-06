#!/usr/bin/env bash
# Fetch a Zig release tarball via the community mirror network and
# verify its minisign signature against the Zig Software Foundation
# public key. Extract into $GITHUB_WORKSPACE/.zig so the alpine
# chroot (which bind-mounts the workspace) can exec it.
#
# Usage:  fetch-zig.sh <version>     e.g. 0.16.0
#
# Why community mirrors instead of ziglang.org:
#   ziglang.org is a single-host service that explicitly disclaims
#   uptime guarantees and is documented to fail "sporadically" under
#   CI load. The ZSF maintains a community-mirror network exactly for
#   this case (see https://ziglang.org/download/community-mirrors).
#
# Why our own script instead of mlugg/setup-zig:
#   mlugg works for current Zig releases but its test matrix tops out
#   at 0.15.2 — Zig's tarball naming has changed enough between minor
#   versions (e.g. zig-linux-x86_64 → zig-x86_64-linux at 0.14.1) that
#   landing 0.16 ahead of mlugg adopting it gave us no clean fallback.
#   The community-mirror protocol is small (~50 lines) and we own it.
#
# Security model:
#   Tarballs are signed by the ZSF and the public key is published on
#   ziglang.org. We pin the key into this script so a compromised
#   mirror can serve any tarball it likes — minisign verification will
#   reject it. We also enforce the `file=` trusted-comment field to
#   prevent downgrade attacks (a mirror serving us 0.5.0 instead of
#   the version we asked for would otherwise verify against the same
#   key).

set -euo pipefail

ZIG_VERSION="${1:?usage: fetch-zig.sh <version>}"

# Pinned ZSF minisign public key, copied from
# https://ziglang.org/download (stable across releases — the ZSF has
# rotated this exactly zero times in the project's history). If a
# rotation ever happens, the website's value becomes the source of
# truth and this script needs a manual update — never trust a key
# served by a mirror.
ZSF_PUBKEY="RWSGOq2NVecA2UPNdBUZykf1CCb147pkmdtYxgb3Ti+JO/wCYvhbAb/U"

# Fallback mirror list — used iff ziglang.org's community-mirrors.txt
# is unreachable (which, per the ZSF docs, can happen). Pulled from
# the canonical list on 2026-05-06; refresh it when bumping
# ZIG_VERSION. We don't need every mirror; we just need at least one
# to be live during a CI run.
FALLBACK_MIRRORS=(
    "https://pkg.hexops.org/zig"
    "https://zigmirror.hryx.net/zig"
    "https://zig.linus.dev/zig"
    "https://zig.squirl.dev"
    "https://ziglang.freetls.fastly.net"
    "https://pkg.earth/zig"
    "https://zig.tilok.dev"
    "https://zigmirror.com"
    "https://zig.chainsafe.dev"
    "https://zig.savalione.com"
)

ARCH="$(uname -m)"
TARBALL="zig-${ARCH}-linux-${ZIG_VERSION}.tar.xz"
SOURCE_TAG="alpine-apks-ci"  # passed via ?source= per ZSF guidance

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# Bring up minisign — distro packages have it everywhere we run, but
# guard the install path for cleanliness.
if ! command -v minisign >/dev/null 2>&1; then
    echo "::group::install minisign"
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq
        sudo apt-get install -y -qq minisign
    elif command -v apk >/dev/null 2>&1; then
        sudo apk add --no-cache minisign
    else
        echo "no supported package manager for minisign install" >&2
        exit 1
    fi
    echo "::endgroup::"
fi

# Step 1 — get the mirror list. If ziglang.org is down (the whole
# reason we're not fetching from it directly), use the baked-in
# fallback list. Either way, shuffle so we don't hammer a single
# mirror across runs (per ZSF's guidance to "shuffle the list").
echo "::group::resolve mirror list"
mirrors_file="$WORKDIR/mirrors.txt"
if curl -fsSL --max-time 10 -o "$mirrors_file" \
    "https://ziglang.org/download/community-mirrors.txt"; then
    echo "fetched canonical mirror list from ziglang.org"
else
    echo "ziglang.org unreachable — using baked-in fallback mirrors"
    printf '%s\n' "${FALLBACK_MIRRORS[@]}" > "$mirrors_file"
fi
shuf "$mirrors_file" > "$WORKDIR/shuffled.txt"
echo "::endgroup::"

# Step 2 — try each mirror in shuffled order, downloading the tarball
# AND its signature, verifying both, before declaring success.
success=0
while IFS= read -r mirror; do
    [ -z "$mirror" ] && continue
    echo "::group::try $mirror"

    if ! curl -fsSL --max-time 60 \
        -o "$WORKDIR/zig.tar.xz" \
        "${mirror}/${TARBALL}?source=${SOURCE_TAG}"; then
        echo "  tarball fetch failed; skipping"
        echo "::endgroup::"
        continue
    fi
    if ! curl -fsSL --max-time 30 \
        -o "$WORKDIR/zig.tar.xz.minisig" \
        "${mirror}/${TARBALL}.minisig?source=${SOURCE_TAG}"; then
        echo "  signature fetch failed; skipping"
        echo "::endgroup::"
        continue
    fi
    if ! minisign -V -P "$ZSF_PUBKEY" \
        -m "$WORKDIR/zig.tar.xz" \
        -x "$WORKDIR/zig.tar.xz.minisig" >/dev/null; then
        echo "  signature verification FAILED; skipping (mirror compromised?)"
        echo "::endgroup::"
        continue
    fi
    # Downgrade-attack guard: the trusted comment of a ZSF-signed
    # tarball includes a `file=<basename>` field. A malicious or
    # misconfigured mirror could serve us a different (older /
    # other-arch) tarball that still verifies against the same key;
    # checking this field rejects that case. minisign itself doesn't
    # parse this field, so we grep it out of the signature file.
    actual_file="$(grep -oE 'file=[^[:space:]]+' "$WORKDIR/zig.tar.xz.minisig" \
        | head -n1 | cut -d= -f2- || true)"
    if [ "$actual_file" != "$TARBALL" ]; then
        echo "  trusted-comment file mismatch: expected '$TARBALL', got '$actual_file'"
        echo "::endgroup::"
        continue
    fi
    echo "  ✓ verified"
    echo "::endgroup::"
    success=1
    break
done < "$WORKDIR/shuffled.txt"

if [ "$success" != 1 ]; then
    echo "::error::All Zig mirrors failed for ${TARBALL}"
    exit 1
fi

# Step 3 — extract into $GITHUB_WORKSPACE/.zig with the canonical
# layout (.zig/zig, .zig/lib/, …) so the rest of the workflow can
# just prepend $GITHUB_WORKSPACE/.zig to PATH.
echo "::group::extract"
tar -xJf "$WORKDIR/zig.tar.xz" -C "$GITHUB_WORKSPACE"
mv "$GITHUB_WORKSPACE/zig-${ARCH}-linux-${ZIG_VERSION}" "$GITHUB_WORKSPACE/.zig"
"$GITHUB_WORKSPACE/.zig/zig" version
echo "::endgroup::"
