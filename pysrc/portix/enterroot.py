"""Enter an unpacked Gentoo root as ``/``, to run Portage inside it.

An ebuild is written against a live root, where its dependencies are installed under
``/usr`` and ``EPREFIX``, ``BROOT`` and ``ESYSROOT`` are all "".  The caller unpacks a
root into a writable directory (a store path cannot be one: overlayfs takes a merged
directory's mode and owner from a read-only, root-owned lower) and this makes it ``/``.

Our uid and gid are mapped to 0: ``emerge`` refuses to merge otherwise, and the root's
files must look root-owned.  It is the one id an unprivileged namespace can map, so
every other id is ``nobody`` in here and nosetid.c answers chown.  The syscalls are
made directly because util-linux checks ``geteuid() == 0`` in userspace, while the
kernel only wants CAP_SYS_ADMIN in the namespace, which unsharing grants.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Final

CLONE_NEWNS: Final = 0x00020000
CLONE_NEWUSER: Final = 0x10000000
MS_BIND: Final = 0x1000
MS_REC: Final = 0x4000
MS_PRIVATE: Final = 0x40000
MNT_DETACH: Final = 0x00000002
#: x86_64; pivot_root has no libc wrapper.
SYS_PIVOT_ROOT: Final = 155
OLD_ROOT: Final = "portix-old-root"

REPO_MOUNT: Final = "var/db/repos/gentoo"
DISTDIR_MOUNT: Final = "var/cache/distfiles"
#: portix's policy, bound over the root's own: with ROOT != "/" Portage builds the
#: build-host config from /etc/portage itself, ignoring PORTAGE_CONFIGROOT, and here
#: the build host is the root being replaced.  A bind, so what a stage merges at
#: /etc/portage is underneath and survives.
CONFIG_MOUNT: Final = "etc/portage"

#: What crosses into the root; everything else is dropped.  Portage saves each ebuild's
#: environment compressed, invisible to nix's reference scanner, and filters only the
#: names it knows, so anything else exported -- out, CUDA_PATH, GIO_EXTRA_MODULES --
#: would carry a store path into the root.  PORTAGE_* is Portage's own and filtered.
ENV_KEEP: Final = ("PATH", "HOME", "TERM", "NIX_BUILD_CORES", "SOURCE_DATE_EPOCH", "ROOT")
ENV_KEEP_PREFIX: Final = "PORTAGE_"


def _check(rc: int, what: str) -> None:
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"portix enterroot: {what}: {os.strerror(err)}")


def _bind(lib: ctypes.CDLL, src: str, target: str, flags: int = MS_BIND | MS_REC) -> None:
    """Recursive by default: a plain bind of /nix drops its submounts, and every store
    binary then reports "No such file or directory" for its own loader."""
    os.makedirs(target, exist_ok=True)
    _check(lib.mount(src.encode(), target.encode(), None, flags, None), f"bind {src}")


def scrub_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k in ENV_KEEP or k.startswith(ENV_KEEP_PREFIX)}


def parse_args(argv: list[str]) -> tuple[str, str, str, str, list[str], list[str]]:
    """``<root> <repo> <distdir> <config> [<carry>...] -- <command>...``"""
    if "--" not in argv:
        raise ValueError("usage: enterroot.py <root> <repo> <distdir> <config> "
                         "[<carry-path>...] -- <command> [args...]")
    sep = argv.index("--")
    if sep < 4:
        raise ValueError(f"portix enterroot: expected at least 4 arguments before --, got {sep}")
    if sep + 1 == len(argv):
        raise ValueError("portix enterroot: no command given")
    return argv[0], argv[1], argv[2], argv[3], argv[4:sep], argv[sep + 1:]


def main(argv: list[str]) -> int:
    try:
        root, repo, distdir, config, carry, command = parse_args(argv)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(root, "usr")):
        print(f"portix enterroot: '{root}' has no usr/ — not an unpacked root", file=sys.stderr)
        return 1

    lib = ctypes.CDLL(None, use_errno=True)
    cwd = os.getcwd()
    uid, gid = os.getuid(), os.getgid()
    _check(lib.unshare(CLONE_NEWUSER | CLONE_NEWNS), "unshare(CLONE_NEWUSER|NEWNS)")
    for name, value in (("setgroups", "deny"), ("uid_map", f"0 {uid} 1"),
                        ("gid_map", f"0 {gid} 1")):
        with open(f"/proc/self/{name}", "w") as fh:
            fh.write(value)
    _check(lib.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None), "make / rprivate")

    # pivot_root wants the new root to be a mount point before its submounts attach.
    _bind(lib, root, root, MS_BIND)
    for host in carry + ["/proc", "/dev"] + (["/sys"] if os.path.isdir("/sys") else []):
        _bind(lib, host, os.path.join(root, host.lstrip("/")))
    for src, mount in ((repo, REPO_MOUNT), (distdir, DISTDIR_MOUNT), (config, CONFIG_MOUNT)):
        _bind(lib, src, os.path.join(root, mount))

    os.makedirs(os.path.join(root, OLD_ROOT), exist_ok=True)
    os.chdir(root)
    lib.syscall.restype = ctypes.c_long
    _check(lib.syscall(SYS_PIVOT_ROOT, b".", OLD_ROOT.encode()), "pivot_root")
    os.chdir("/")
    _check(lib.umount2(f"/{OLD_ROOT}".encode(), MNT_DETACH), "detach old root")
    os.rmdir(f"/{OLD_ROOT}")
    # The cwd keeps its absolute name only if it is under a carried path ($NIX_BUILD_TOP).
    if not os.path.isdir(cwd):
        print(f"portix enterroot: cwd '{cwd}' is not inside the new root — carry it",
              file=sys.stderr)
        return 1
    os.chdir(cwd)
    os.execve(command[0], command, scrub_env(dict(os.environ)))
