"""Lift one merged package out of the root, by its own CONTENTS.

``CONTENTS`` is Portage's record of exactly what it merged, so it is narrower and more
truthful than any diff of the root -- which would also catch an ``env-update``, a
regenerated cache, another package's postinst.  What a node publishes:

  image/                   the files, in the root's own merged-usr layout
  image/var/db/pkg/<cpv>/  the vdb entry: USE, NEEDED, CONTENTS, the ebuild itself
  launchers                the package's own programs, NUL-terminated
  identity/<file>          the lines this merge added to passwd, group, shadow and
                           gshadow, which a dependent replays (they are append-only)
  identity/dropped         every owner and setuid/setgid request the store could not
                           hold, against the image path it named
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Final

from portix import contents

VDB: Final = "var/db/pkg"
#: Where a consumer should be able to run a package's programs from.
LAUNCHABLE: Final = ("usr/bin/", "usr/sbin/")
IDENTITY: Final = ("passwd", "group", "shadow", "gshadow")
#: make.conf's PORTAGE_TMPDIR; Portage stages an install at portage/<cpv>/image under it.
PORTAGE_TMPDIR: Final = "/var/tmp"


def lift(root: str, out: str, entries: list[contents.Entry],
         bridge: dict[str, str]) -> tuple[int, int, int, list[str], list[str]]:
    """Copy what CONTENTS claims, under the root's merged-usr layout: copied
    literally, ``/bin/getfacl`` would make a real ``bin/`` that, stacked over the
    root, replaces its ``bin`` symlink, after which ``/bin/sh`` does not exist.

    A claimed file that is absent is reported, not
    fatal: a postinst may remove what the merge recorded (glib's giomodule.cache).
    A fifo or device node is reported, not copied: the store holds only files,
    directories and symlinks."""
    files = links = dirs = 0
    absent: list[str] = []
    special: list[str] = []
    for e in entries:
        rel = e.path.lstrip("/")
        src, dst = os.path.join(root, rel), os.path.join(out, contents.normalize(rel, bridge))
        if e.kind == "dir":
            os.makedirs(dst, exist_ok=True)
            dirs += 1
        elif e.kind == "sym":
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.lexists(dst):
                os.symlink(e.target, dst)
            links += 1
        elif e.kind == "obj":
            if not os.path.exists(src):
                absent.append(e.path)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            files += 1
        else:
            special.append(f"{e.kind} {e.path}")
    return files, links, dirs, absent, special


def launchables(entries: list[contents.Entry], bridge: dict[str, str]) -> list[str]:
    """The package's OWN programs.  Scanning an image gave chromium 221 launchers --
    ``dbus-daemon``, ``cupsd`` -- that on PATH shadow the system's."""
    out: set[str] = set()
    for e in entries:
        if e.kind in ("obj", "sym"):
            rel = contents.normalize(e.path.lstrip("/"), bridge)
            if rel.startswith(LAUNCHABLE) and "/" not in rel.split("/", 2)[2]:
                out.add(rel)
    return sorted(out)


def identity_added(root: str, base: str, name: str) -> list[str]:
    """The lines a merge added to one identity file.  acct-user and acct-group mutate
    files baselayout owns, so the effect is in nobody's CONTENTS."""
    with open(os.path.join(base, name), encoding="utf-8") as fh:
        before = set(fh.read().splitlines())
    with open(os.path.join(root, "etc", name), encoding="utf-8") as fh:
        return [line for line in fh.read().splitlines() if line not in before]


def dropped(log: str, cpv: str, claimed: set[str],
            bridge: dict[str, str]) -> tuple[list[str], int]:
    """(rows naming this package's content, how many named anything else).

    A request is the package's when its path, read through the install image while
    staging and through the root after the merge, is one CONTENTS claims.  That covers
    fperms in src_install and fcaps' chmod in pkg_postinst alike; everything else --
    the unpacked source, Portage's own directories -- is the build environment,
    counted and not shipped.
    """
    if not os.path.exists(log):
        return [], 0
    image = os.path.join(PORTAGE_TMPDIR, "portage", cpv, "image")
    rows: set[str] = set()
    other = 0
    with open(log, encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            kind, request, where = line.rstrip("\n").split("\t")
            path = os.path.normpath(where)
            staged = path.startswith(image + "/")
            rel = contents.normalize(os.path.relpath(path, image) if staged else path.lstrip("/"),
                            bridge)
            if rel in claimed:
                rows.add(f"{kind}\t{request}\t{rel}")
            else:
                other += 1
    return sorted(rows), other


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: emit.py <root> <out> <cpv> <nosetid-log> <identity-base>",
              file=sys.stderr)
        return 2
    root, out, cpv, log, base = argv
    entry = os.path.join(root, VDB, cpv)
    entries = contents.read(os.path.join(entry, "CONTENTS"))
    bridge = contents.bridges(root)

    image = os.path.join(out, "image")
    os.makedirs(image)
    files, links, dirs, absent, special = lift(root, image, entries, bridge)
    shutil.copytree(entry, os.path.join(image, VDB, cpv))
    with open(os.path.join(out, "launchers"), "w", encoding="utf-8") as fh:
        fh.write("".join(rel + "\0" for rel in launchables(entries, bridge)))

    identity = os.path.join(out, "identity")
    os.makedirs(identity)
    for name in IDENTITY:
        with open(os.path.join(identity, name), "w", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in identity_added(root, base, name)))
    claimed = {contents.normalize(e.path.lstrip("/"), bridge) for e in entries}
    rows, other = dropped(log, cpv, claimed, bridge)
    with open(os.path.join(identity, "dropped"), "w", encoding="utf-8") as fh:
        fh.write("".join(row + "\n" for row in rows))

    for path in absent:
        print(f"portix: claimed and absent after the merge — {path}")
    for what in special:
        print(f"portix: not content, not emitted — {what}")
    for row in rows:
        print(f"portix: identity the store cannot hold — {row}")
    print(f"portix: emitted {cpv} — {files} files, {links} symlinks, {dirs} directories"
          f"; {len(rows)} identity requests recorded, {other} in the build environment")
    return 0
