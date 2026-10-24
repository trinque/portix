"""What a root must be, asserted inside the build that produces it.

``vdb-consistent`` (fatal): every path some ``CONTENTS`` claims exists.  A merge that
half happened, or a file a later package clobbered, shows up here and nowhere else.

``no-store-paths`` (fatal): no file names a store path other than the root itself.  A
compressed file is read decompressed: compression hides a reference from nix's own
scanner, which is how a leaked ``PYTHONPATH`` once reached every ``environment.bz2``.

``unowned`` (a census, not a fault): ``pkg_postinst`` creates files no ``CONTENTS``
claims -- gcc-config's compiler symlinks, ``/etc/ld.so.conf``, the account database --
on any Gentoo root.  Printed with its count so growth stays visible.

The checks run inside the rung's own build on purpose: there ``/nix/store`` is exactly
the rung's inputs, so "is this hash visible" is "is this one of my inputs", and a
string that merely looks like a store path is not mistaken for a reference.
"""

from __future__ import annotations

import bz2
import collections
import gzip
import io
import lzma
import os
import re
import sys
from compression import zstd
from typing import Callable, Final, Iterator

from portix import contents

#: Not merged content: Portage's own bookkeeping, and mount points.
UNWALKED: Final = (
    "var/db/pkg", "var/cache/edb", "var/lib/portage", "var/log", "var/tmp",
    "tmp", "run", "dev", "proc", "sys",
)

#: A store REFERENCE: ``/nix/store/`` plus 32 characters of nix's base32 and a ``-``,
#: which is what nix's own scanner looks for.  ``sys-apps/file``'s magic database
#: contains the bare string ``#!/nix/store/``, and that is not one.
STORE_REF: Final = re.compile(rb"/nix/store/([0-9a-df-np-sv-z]{32})-")
_MARKER: Final = b"/nix/store/"
_OVERLAP: Final = len(_MARKER) + 33

_OPENERS: Final[tuple[tuple[bytes, Callable[[str], io.BufferedIOBase]], ...]] = (
    (b"BZh", lambda p: bz2.open(p, "rb")),
    (b"\x1f\x8b", lambda p: gzip.open(p, "rb")),
    (b"\xfd7zXZ\x00", lambda p: lzma.open(p, "rb")),
    (b"(\xb5/\xfd", lambda p: zstd.open(p, "rb")),
)


def contents_entries(root: str) -> dict[str, list[str]]:
    """Every path some package's CONTENTS claims, mapped to the packages claiming it."""
    vdb = os.path.join(root, "var/db/pkg")
    claimed: dict[str, list[str]] = collections.defaultdict(list)
    for category in sorted(os.listdir(vdb)):
        if not os.path.isdir(os.path.join(vdb, category)):
            continue
        for pf in sorted(os.listdir(os.path.join(vdb, category))):
            path = os.path.join(vdb, category, pf, "CONTENTS")
            if not os.path.exists(path):
                raise SystemExit(f"portix check: {category}/{pf} has no CONTENTS")
            for entry in contents.read(path):
                claimed[entry.path.lstrip("/")].append(f"{category}/{pf}")
    return claimed


def walk_paths(root: str) -> Iterator[str]:
    """Every path in the root, relative, skipping the unwalked directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else rel
        dirnames[:] = [d for d in dirnames if os.path.join(rel, d) not in UNWALKED]
        for name in dirnames + filenames:
            yield os.path.join(rel, name) if rel else name


def missing(root: str, claimed: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Claimed paths not in the root.  ``lexists``: ``usr/bin/gcc`` points into a
    gcc-bin directory that resolves only when this tree is ``/``."""
    return [(path, pkgs[0]) for path, pkgs in sorted(claimed.items())
            if not os.path.lexists(os.path.join(root, path))]


def unowned(root: str, claimed: dict[str, list[str]]) -> list[str]:
    """Paths no claim names, a claim read through the merged-usr links: Portage
    records ``/sbin/agetty`` for the file at ``usr/bin/agetty``."""
    bridge = contents.bridges(root)
    named = set(claimed) | {contents.normalize(p, bridge) for p in claimed}
    return sorted(p for p in walk_paths(root) if p not in named)


def visible_hashes() -> set[bytes]:
    """The store hashes this build can see, which inside a sandbox is its inputs."""
    return {name.split("-", 1)[0].encode() for name in os.listdir("/nix/store")}


def own_hash(root: str) -> bytes | None:
    """The root's own hash, when it is a store path: ``--root=$out`` records itself
    (``KERNEL_DIR``, mtimedb), and nix does not call a self-reference a dependency."""
    head = os.path.basename(root.rstrip("/")).split("-", 1)[0].encode()
    return head if STORE_REF.fullmatch(b"/nix/store/" + head + b"-") else None


def _refs(stream: io.BufferedIOBase, own: bytes | None, visible: set[bytes]) -> set[bytes]:
    found: set[bytes] = set()
    tail = b""
    while chunk := stream.read(1 << 22):
        window = tail + chunk
        if _MARKER in window:
            found |= {m.group(1) for m in STORE_REF.finditer(window)
                      if m.group(1) != own and m.group(1) in visible}
        tail = window[-_OVERLAP:]
    return found


def file_refs(path: str, own: bytes | None, visible: set[bytes]) -> set[bytes]:
    """The foreign store hashes a file names, read through any compression.

    A magic number is a guess about a file's format; when the guess is wrong the file
    is scanned as the bytes it is, so a wrong guess can narrow nothing."""
    with open(path, "rb") as fh:
        magic = fh.read(6)
    for prefix, opener in _OPENERS:
        if magic.startswith(prefix):
            try:
                with opener(path) as stream:
                    return _refs(stream, own, visible)
            except (OSError, EOFError, lzma.LZMAError, zstd.ZstdError):
                break
    with open(path, "rb") as fh:
        return _refs(fh, own, visible)


def store_paths(root: str) -> list[str]:
    own, visible = own_hash(root), visible_hashes()
    hits: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path) and file_refs(path, own, visible):
                hits.append(os.path.relpath(path, root))
    return sorted(hits)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check.py <root>", file=sys.stderr)
        return 2
    root = argv[0]
    claimed = contents_entries(root)
    failed = False

    gone = missing(root, claimed)
    if gone:
        failed = True
        print(f"portix: {len(gone)} paths a CONTENTS claims are not in the root:",
              file=sys.stderr)
        for path, pkg in gone[:100]:
            print(f"  {path}  ({pkg})", file=sys.stderr)

    leaks = store_paths(root)
    if leaks:
        failed = True
        print(f"portix: {len(leaks)} files in the root name a store path:", file=sys.stderr)
        for path in leaks[:50]:
            print(f"  {path}", file=sys.stderr)

    orphans = unowned(root, claimed)
    print(f"portix: {len(orphans)} paths claimed by no CONTENTS (pkg_postinst's work):")
    for where, count in collections.Counter(
            "/".join(p.split("/")[:2]) for p in orphans).most_common():
        print(f"  {count:5d}  {where}")
    if failed:
        return 1
    print(f"portix: root verified — {len(claimed)} claimed paths present, no store path")
    return 0
