"""Run a portix-built program with its own root as ``/``.

A portix binary is linked and configured against a real FHS root -- ``PT_INTERP``
``/lib64/ld-linux-x86-64.so.2``, figlet's fonts at ``/usr/share/figlet`` baked into
the binary -- and no relocation reaches every absolute path a package bakes in.  So
the launcher gives the program the root it was built for: a user + mount namespace,
the root and the package's runtime closure stacked read-only with overlayfs, the
host's ``/nix``, ``/home``, ``/tmp``, kernel filesystems and session sockets bound back
in, ``pivot_root``, exec.

The uid is mapped to itself, not to 0: running a program is not a superuser action,
and a launcher that made you root inside would be lying about who you are.

Needs Linux >= 6.8 (``lowerdir+``, one overlay layer per call) and unprivileged user
namespaces that may mount (refused where AppArmor restricts them, as Ubuntu does by
default).
"""

from __future__ import annotations

import ctypes
import os
import shlex
import sys
import tempfile
from typing import Final

CLONE_NEWNS: Final = 0x00020000
CLONE_NEWUSER: Final = 0x10000000
MS_BIND: Final = 0x1000
MS_REC: Final = 0x4000
MS_PRIVATE: Final = 0x40000
MNT_DETACH: Final = 0x00000002

#: x86_64 numbers; pivot_root has no libc wrapper, nor does the new mount API.
SYS_PIVOT_ROOT: Final = 155
SYS_MOVE_MOUNT: Final = 429
SYS_FSOPEN: Final = 430
SYS_FSCONFIG: Final = 431
SYS_FSMOUNT: Final = 432
FSCONFIG_SET_FLAG: Final = 0
FSCONFIG_SET_STRING: Final = 1
FSCONFIG_CMD_CREATE: Final = 6
MOUNT_ATTR_RDONLY: Final = 0x1
MOVE_MOUNT_F_EMPTY_PATH: Final = 0x4
AT_FDCWD: Final = -100

#: Host paths bound back in at the same path: the minimum, to which a caller adds its
#: own with --bind.  ``/etc`` is the ROOT's, where the package's configuration lives --
#: but NixOS's ``/etc`` is a symlink farm into ``/etc/static``, so a caller-named
#: ``/etc`` path dangles without it.  A path absent on the host is skipped.
BIND: Final = ("/nix", "/home", "/tmp", "/proc", "/dev", "/sys", "/run",
               "/etc/resolv.conf", "/etc/static")


def _libc() -> ctypes.CDLL:
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    return lib


def _check(rc: int, what: str) -> None:
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"portix launch: {what}: {os.strerror(err)}")


def unshare_as_self(lib: ctypes.CDLL) -> None:
    uid, gid = os.getuid(), os.getgid()
    _check(lib.unshare(CLONE_NEWUSER | CLONE_NEWNS), "unshare(CLONE_NEWUSER|NEWNS) (unprivileged user namespaces disabled?)")
    with open("/proc/self/setgroups", "w") as fh:
        fh.write("deny")
    with open("/proc/self/uid_map", "w") as fh:
        fh.write(f"{uid} {uid} 1")
    with open("/proc/self/gid_map", "w") as fh:
        fh.write(f"{gid} {gid} 1")


def mount_overlay(lib: ctypes.CDLL, layers: list[str], target: str) -> None:
    """Stack ``layers`` read-only at ``target``, leftmost winning.

    One ``lowerdir+`` per layer: ``mount(2)`` copies its options through one page, and
    chromium's 89 store-path layers are 5984 bytes, which the kernel truncated
    mid-path.  Read-only, so no upper, no workdir; ``userxattr`` is what an
    unprivileged namespace may mount with."""
    fd = lib.syscall(SYS_FSOPEN, b"overlay", 0)
    _check(fd, "fsopen(overlay)")
    for layer in layers:
        _check(lib.syscall(SYS_FSCONFIG, fd, FSCONFIG_SET_STRING, b"lowerdir+",
                           layer.encode(), 0), f"fsconfig lowerdir+ {layer} (needs Linux >= 6.8)")
    _check(lib.syscall(SYS_FSCONFIG, fd, FSCONFIG_SET_FLAG, b"userxattr", None, 0),
           "fsconfig userxattr")
    _check(lib.syscall(SYS_FSCONFIG, fd, FSCONFIG_CMD_CREATE, None, None, 0),
           f"overlay of {len(layers)} layers")
    mfd = lib.syscall(SYS_FSMOUNT, fd, 0, MOUNT_ATTR_RDONLY)
    _check(mfd, "fsmount(overlay)")
    _check(lib.syscall(SYS_MOVE_MOUNT, mfd, b"", AT_FDCWD, target.encode(),
                       MOVE_MOUNT_F_EMPTY_PATH), f"move_mount -> {target}")
    os.close(mfd)
    os.close(fd)


def bind_paths(extra: list[str]) -> list[str]:
    return list(BIND) + [p for p in extra if p not in BIND]


def mountpoint_layer(where: str, paths: list[str]) -> str:
    """A lowest layer holding the mountpoints the root lacks (``/nix`` among them --
    the emit drops it).  A lower layer, because the merged view is read-only."""
    for host in paths:
        target = os.path.join(where, host.lstrip("/"))
        if os.path.isdir(host):
            os.makedirs(target, exist_ok=True)
        elif os.path.exists(host):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            open(target, "w").close()
    return where


def bind_host(lib: ctypes.CDLL, root: str, paths: list[str]) -> None:
    for host in paths:
        target = os.path.join(root, host.lstrip("/"))
        if os.path.exists(host) and os.path.exists(target):
            _check(lib.mount(host.encode(), target.encode(), None, MS_BIND | MS_REC, None),
                   f"bind {host}")


def enter(lib: ctypes.CDLL, root: str) -> None:
    """pivot_root, never chroot: the kernel refuses CLONE_NEWUSER to a chrooted
    process, which takes a nested sandbox away from the program (chromium falls back to
    a setuid helper the store cannot carry, and aborts).  ``put_old`` may be
    ``new_root`` itself, so nothing in the read-only merge needs to be writable."""
    os.chdir(root)
    _check(lib.syscall(SYS_PIVOT_ROOT, b".", b"."), "pivot_root(., .)")
    _check(lib.umount2(b".", MNT_DETACH), "umount2(., MNT_DETACH)")
    os.chdir("/")


# --- /etc/env.d -------------------------------------------------------------------
#: Portage's rules for the variables that accumulate across fragments
#: (``portage/util/env_update.py``); a fragment may declare more, and xdg-utils does.
COLON_SEPARATED: Final = frozenset({
    "ADA_INCLUDE_PATH", "ADA_OBJECTS_PATH", "CLASSPATH", "INFODIR", "INFOPATH",
    "KDEDIRS", "LDPATH", "MANPATH", "PATH", "PKG_CONFIG_PATH", "PRELINK_PATH",
    "PRELINK_PATH_MASK", "PYTHONPATH", "ROOTPATH",
})
SPACE_SEPARATED: Final = frozenset({"CONFIG_PROTECT", "CONFIG_PROTECT_MASK"})

#: The XDG specification's defaults, applied only where no fragment in the closure
#: states the variable: the caller's value is host paths, and inherited it masks the
#: default a library would otherwise use (GLib then finds no schemas).
XDG_DEFAULTS: Final = {"XDG_DATA_DIRS": "/usr/local/share:/usr/share",
                       "XDG_CONFIG_DIRS": "/etc/xdg"}


def envd_fragments(envd: str) -> list[str]:
    """Fragments in Portage's order: two leading digits, sorted; ``gcc``, ``binutils``
    and friends in the same directory are eselect's bookkeeping."""
    if not os.path.isdir(envd):
        return []
    return [os.path.join(envd, n) for n in sorted(os.listdir(envd))
            if len(n) >= 3 and n[:2].isdigit() and not n.endswith(("~", ".bak"))]


def envd_parse(text: str) -> dict[str, str]:
    """KEY=VALUE lines, shell-quoted: ``LESS="-R -M --shift 5"`` is one value."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        key, eq, value = line.partition("=")
        if line.startswith("#") or not eq or not key.replace("_", "").isalnum():
            continue
        out[key] = " ".join(shlex.split(value, comments=True))
    return out


def envd_aggregate(envd: str) -> tuple[dict[str, str], frozenset[str]]:
    """``env-update``'s fold, and which variables accumulate: accumulating ones keep
    first-seen order without duplicates, the rest are last-fragment-wins.

    Portage's own run of this, over the same closure, is in the aggregate layer as
    ``/etc/profile.env``; this keeps the second answer that file does not record."""
    parsed: list[dict[str, str]] = []
    for path in envd_fragments(envd):
        with open(path, encoding="utf-8", errors="replace") as fh:
            parsed.append(envd_parse(fh.read()))
    colon, space = set(COLON_SEPARATED), set(SPACE_SEPARATED)
    for frag in parsed:
        colon.update(frag.pop("COLON_SEPARATED", "").split())
        space.update(frag.pop("SPACE_SEPARATED", "").split())
    env: dict[str, str] = {}
    for names, sep in ((space, " "), (colon, ":")):
        for var in names:
            values: list[str] = []
            for frag in parsed:
                for item in frag.pop(var, "").split(sep):
                    if item and item not in values:
                        values.append(item)
            if values:
                env[var] = sep.join(values)
    for frag in parsed:
        env.update(frag)
    return env, frozenset(colon)


def repair_env(envd: str = "/etc/env.d") -> None:
    """The environment a root program sees.

    The root's loader is configured by the root's ``/etc/ld.so.cache`` -- which the
    aggregate layer regenerates over the stacked closure -- so every host ``LD_*``
    variable is dropped: it configures the host's loader, for host objects.  An
    accumulating variable takes the root's value first and keeps the caller's after
    (a ``sh -c`` inside must find the root's ``pkg-config``); a plain one the caller
    set is the caller's statement and is left alone.  TMPDIR is repaired when it names
    a directory that is not here; HOME never is.
    """
    for var in [v for v in os.environ if v.startswith("LD_")]:
        del os.environ[var]
    env, accumulating = envd_aggregate(envd)
    env.pop("LDPATH", None)
    if "PATH" not in env:
        raise SystemExit("portix launch: /etc/env.d declares no PATH, so this stack is "
                         "not a root (baselayout's 50baselayout always declares it)")
    for var, default in XDG_DEFAULTS.items():
        env.setdefault(var, default)
    accumulating |= frozenset(XDG_DEFAULTS)
    for var, value in env.items():
        if var in accumulating:
            mine = value.split(":")
            kept = [p for p in os.environ.get(var, "").split(":") if p and p not in mine]
            os.environ[var] = ":".join(mine + kept)
        elif var in SPACE_SEPARATED or var not in os.environ:
            os.environ[var] = value
    if not os.path.isdir(os.environ.get("TMPDIR", "/tmp")):
        os.environ["TMPDIR"] = "/tmp"


def parse_args(argv: list[str]) -> tuple[list[str], list[str], list[str]]:
    """``[--bind PATH]... <root> [<layer>...] -- <command> [args...]`` into (layers,
    command, binds).  Layers are given root first and returned reversed, so the last
    one given wins in the overlay."""
    if "--" not in argv:
        raise ValueError("usage: launch.py [--bind PATH]... <root> [<layer>...] "
                         "-- <command> [args...]")
    extra: list[str] = []
    while len(argv) >= 2 and argv[0] == "--bind":
        extra.append(argv[1])
        argv = argv[2:]
    sep = argv.index("--")
    layers, command = argv[:sep], argv[sep + 1:]
    if not layers:
        raise ValueError("portix launch: no root given")
    if not command:
        raise ValueError("portix launch: no command given")
    return list(reversed(layers)), command, extra


def main(argv: list[str]) -> int:
    try:
        layers, command, extra = parse_args(argv)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    for layer in layers:
        if not os.path.isdir(layer):
            print(f"portix launch: '{layer}' is not a directory", file=sys.stderr)
            return 1

    lib = _libc()
    cwd = os.getcwd()
    # Scratch for the merge point and the mountpoint layer: a private directory with a
    # tmpfs over it that exists only in this namespace.  Under /tmp, which is bound
    # into the root, so it can be detached and removed from inside before the exec.
    scratch = tempfile.mkdtemp(prefix="portix-", dir="/tmp")
    unshare_as_self(lib)
    _check(lib.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None), "make / rprivate (user namespaces restricted, e.g. by AppArmor?)")
    _check(lib.mount(b"tmpfs", scratch.encode(), b"tmpfs", 0, b"mode=0700"), "tmpfs scratch")
    merged = os.path.join(scratch, "root")
    os.mkdir(merged)
    paths = bind_paths(extra)
    mount_overlay(lib, layers + [mountpoint_layer(os.path.join(scratch, "mnt"), paths)],
                  merged)
    bind_host(lib, merged, paths)
    enter(lib, merged)
    _check(lib.umount2(scratch.encode(), MNT_DETACH), "detach scratch")
    os.rmdir(scratch)
    repair_env()
    os.chdir(cwd if os.path.isdir(cwd) else "/")
    os.execvp(command[0], command)
