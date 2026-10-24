"""Regenerate what is a fold over the installed set, by re-running Portage over a closure.

A file that is a join over every installed package -- ``gschemas.compiled``,
``mime.cache``, ``icon-theme.cache``, ``ld.so.cache`` -- is in nobody's ``CONTENTS``,
so no node emits it, and stacking each package's own snapshot of it picks a winner
where the answer is a union.  So it is regenerated, by the generator Gentoo already
recorded: each stacked package's own ``pkg_postinst`` (its ebuild and saved
environment ride in its vdb entry), in Portage's merge order, then Portage's own
``env_update`` for ``/etc/env.d``.  No table of caches, so no per-ebuild knowledge.

The layer this writes is every path those runs created or rewrote, stacked last by a
launcher so the union beats each package's snapshot.

The base root's packages are not re-run: their postinsts ran in place when the ladder
built the root, and seven of them (the docbook/sgml catalogue family) read ``$T``,
the build tree, which is gone once a package is merged.  So a stacked package feeding
an aggregate only a BASE package's postinst regenerates leaves that aggregate stale.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Final

from portix import check

#: The namespace's own binds: present only while we are inside it, and not content.
#: Named, because ``os.path.ismount`` cannot see a bind (same st_dev as its parent).
NOT_CONTENT: Final = ("nix", "home", "var/db/repos", "var/cache/distfiles", "etc/portage")

#: Host state a postinst MINTS rather than derives (``dbus-uuidgen``).  Baked into a
#: layer it would give every host the same machine id.
MINTED: Final = ("etc/machine-id", "var/lib/dbus/machine-id")


def installed(root: str) -> set[str]:
    vdb = os.path.join(root, "var/db/pkg")
    return {f"{c}/{pf}" for c in os.listdir(vdb) if os.path.isdir(os.path.join(vdb, c))
            for pf in os.listdir(os.path.join(vdb, c))}


def with_postinst(root: str, base: set[str]) -> list[str]:
    """The stacked packages that define a ``pkg_postinst``, in merge order (COUNTER).

    Order matters: ``gtk+``'s postinst runs ``gtk-query-immodules-3.0``, which needs
    pango present."""
    found: list[tuple[int, str]] = []
    for catpf in sorted(installed(root) - base):
        entry = os.path.join(root, "var/db/pkg", catpf)
        with open(os.path.join(entry, "DEFINED_PHASES"), encoding="utf-8") as fh:
            if "postinst" not in fh.read().split():
                continue
        with open(os.path.join(entry, "COUNTER"), encoding="utf-8") as fh:
            found.append((int(fh.read()), catpf))
    return [catpf for _, catpf in sorted(found)]


def snapshot(root: str, carried: str) -> dict[str, tuple[int, int, int]]:
    """Every path that could be content, with what says whether it was rewritten.

    The inode is in the key because a cache is written to a temporary name and
    renamed over the old one, which can leave size and mtime unchanged."""
    skip = set(check.UNWALKED) | set(NOT_CONTENT) | {carried.strip("/")}
    seen: dict[str, tuple[int, int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else rel
        dirnames[:] = [d for d in dirnames if os.path.join(rel, d) not in skip]
        for name in filenames:
            path = os.path.join(rel, name)
            st = os.lstat(os.path.join(root, path))
            seen[path] = (st.st_ino, st.st_size, st.st_mtime_ns)
        for name in dirnames:
            seen[os.path.join(rel, name)] = (0, 0, 0)
    return seen


def run(argv: list[str], what: str) -> None:
    result = subprocess.run(argv, cwd="/", check=False)
    if result.returncode != 0:
        raise SystemExit(
            f"portix aggregate: {what} failed (exit {result.returncode}). A phase that "
            "runs a program the closure does not fully carry fails here; so does a "
            "postinst that reads $T, which cannot be re-run after the merge at all.")


def emit_delta(root: str, before: dict[str, tuple[int, int, int]], after: dict[str, tuple[int, int, int]],
               out: str) -> tuple[int, int, int]:
    files = links = dirs = 0
    for rel in sorted(p for p, key in after.items() if before.get(p) != key):
        if rel in MINTED:
            continue
        src, dst = os.path.join(root, rel), os.path.join(out, rel)
        if os.path.islink(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(os.readlink(src), dst)
            links += 1
        elif os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            dirs += 1
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            files += 1
        else:
            print(f"portix aggregate: not content, not emitted — {rel}")
    return files, links, dirs


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: aggregate.py <out> <portage-bin> <portage-pym> <base-root> "
              "<carried-build-dir>", file=sys.stderr)
        return 2
    out, portage_bin, portage_pym, base_root, carried = argv
    packages = with_postinst("/", installed(base_root))
    print(f"portix aggregate: {len(packages)} stacked packages define a pkg_postinst",
          flush=True)
    before = snapshot("/", carried)
    for catpf in packages:
        ebuild = os.path.join("/var/db/pkg", catpf, catpf.split("/", 1)[1] + ".ebuild")
        run([os.path.join(portage_bin, "ebuild"), ebuild, "postinst"], f"{catpf} pkg_postinst")
    run(["/usr/bin/python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
         "import portage; portage.env_update()", portage_pym], "env_update")
    files, links, dirs = emit_delta("/", before, snapshot("/", carried), out)
    print(f"portix aggregate: {files} files, {links} symlinks, {dirs} directories "
          f"regenerated over {len(packages)} postinsts and env_update")
    return 0
