"""Compare two stage roots, and say exactly what moved.

WHAT THIS PROVES, AND WHAT IT DOES NOT.  It proves the ladder stopped moving.  It is
NOT the discard proof and must never be cited as one: a constant carried by every rung
*is* a fixed point, so convergence cannot see one.  What rules out carry-forward is
structural — stage1's ``$out`` starts empty, and rebuild1's ``/`` is stage1.

WHY IT IS ITS OWN DERIVATION.  The exclusion set below is the part that will churn,
and every widening of it is an admission.  Keeping it out of the stage derivations
means changing it costs a digest pass, not a rung — so there is never a reason to wave
a difference through because re-running would be expensive.

Usage: ``diff.py <root-a> <root-b>``.
"""

from __future__ import annotations

import bz2
import fnmatch
import hashlib
import os
import re
import sys
from typing import Final, Iterator

#: Trees whose contents are a record of the run rather than of the system.  Each is a
#: prefix, and each is here for a reason that would survive being asked about.
EXCLUDED_PREFIXES: Final = (
    # emerge.log, the elog directory, sandbox logs: this build's diary, wall-clock by
    # nature.
    "var/log",
    # Portage's caches. Dropped at emit already; named here so a root that still has
    # them (one built by hand, say) compares the same way.
    "var/cache/edb",
    # PORTAGE_TMPDIR. Likewise dropped at emit.
    "var/tmp",
    # ldconfig's aux-cache. Also a cache, also dropped at emit, and named here so a
    # root built by hand compares the same way.
    "var/cache/ldconfig",
)

#: Per-package vdb files that record WHEN rather than WHAT.  Basenames under
#: ``var/db/pkg/<cat>/<pf>/``, and the list is short on purpose.
VDB_VOLATILE: Final = (
    "BUILD_TIME",  # seconds since the epoch, by definition
    "COUNTER",     # Portage's merge serial; increments with every merge, ever
)

VDB_PREFIX: Final = "var/db/pkg/"

#: Portage (from 2026-10) also writes an entry's keys into one ``metadata`` file, one
#: ``KEY=value`` line each, plus ``#dir_mtime=``.  The same keys are volatile there,
#: and are dropped line by line, so every other key in it still compares.
VDB_METADATA: Final = "metadata"
METADATA_VOLATILE: Final = VDB_VOLATILE + ("#dir_mtime",)

#: vdb records whose LINE ORDER carries no information.  These are NORMALIZED, not
#: excluded — the same bytes are still compared, with only the incidental ordering
#: removed, so a genuinely changed soname or checksum still fails.
#:
#: MEASURED on the first full comparison: all 142 differing NEEDED and NEEDED.ELF.2
#: files differed ONLY in line order (scanelf walks the image in readdir order), and
#: 222 of 268 differing CONTENTS only in order and mtime.  Excluding the files instead
#: would have hidden the 2 that differed for real.
VDB_UNORDERED: Final = ("NEEDED", "NEEDED.ELF.2", "CONTENTS")

#: Portage's saved ebuild environment, compared DECOMPRESSED and with the lines below
#: dropped.  Decompressed because bz2 framing is not the thing being compared;
#: line-dropped because a shell variable that outlives its loop is a record of how the
#: build walked a directory, not of what it produced.
VDB_ENVIRONMENT: Final = "environment.bz2"

#: Environment lines whose value is loop-order-dependent.  Literal prefixes, each with
#: its mechanism read from the source — never a general "looks transient" rule.
#:
#: `mod_lines`: cmake.eclass:431-437 loops over CMakeLists.txt files and does
#: `readarray -t mod_lines` INSIDE the loop without declaring it local, so it survives
#: holding whatever the LAST file processed yielded — and the loop order is find's
#: readdir order.  MEASURED: dev-libs/json-c-0.18 was the single difference between
#: two converged rungs on a full ladder, `mod_lines=([0]=$'#_cmake_modify_IGNORE
#: \tset(CMAKE_BUILD_TYPE debug)')` against `mod_lines=()`, with every installed file
#: identical.  A different pair of rungs showed no difference at all, which is what
#: "sporadic" looks like.
ENVIRONMENT_VOLATILE: Final = ("declare -a mod_lines=",)

#: The trailing mtime on a CONTENTS ``obj``/``sym`` line: the wall clock at merge, and
#: the only field on it that is not a fact about the file.
CONTENTS_MTIME: Final = re.compile(r" [0-9]{9,}$")

#: Files that differ between any two rungs because the PACKAGE is not reproducible,
#: not because portix is.  Each is a path pattern with its cause, and each is REPORTED
#: on every run rather than dropped -- an admission stays visible or it stops being
#: one.  Patterns, not paths, because the tree moves: a version bump must not turn an
#: admitted nondeterminism into a failure.  An admission that matches nothing is
#: reported too, so the list cannot rot.
KNOWN_UPSTREAM: Final = {
    # perl's Configure stamps the build clock into its own config, and perlbug and
    # perlthanks embed that config verbatim. MEASURED, the whole of the difference:
    #   -  * Configuration time: Wed Aug  5 17:59:19 UTC 2026
    #   +  * Configuration time: Wed Aug  5 19:16:14 UTC 2026
    "usr/bin/perlbug": "perl Configure stamps 'Configuration time' into its config",
    "usr/bin/perlthanks": "a copy of perlbug, same stamp",
    "usr/lib64/perl5/*/x86_64-linux/CORE/config.h": "perl Configure stamps 'Configuration time' here",
    "usr/lib64/perl5/*/x86_64-linux/Config_heavy.pl": "perl Configure stamps 'Configuration time' here",
}


def admitted(rel: str) -> str | None:
    """The admission a path falls under, if any."""
    return next((pattern for pattern in KNOWN_UPSTREAM
                 if fnmatch.fnmatchcase(rel, pattern)), None)


def excluded(rel: str) -> bool:
    if any(rel == p or rel.startswith(p + "/") for p in EXCLUDED_PREFIXES):
        return True
    if rel.startswith(VDB_PREFIX) and os.path.basename(rel) in VDB_VOLATILE:
        return True
    return False


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _contents_line(line: str) -> str:
    """A CONTENTS line with its mtime gone, and its md5 gone IF the file it records is
    an admitted upstream nondeterminism.

    Dropping the md5 only for those paths is the point: the record still compares in
    full for every other file in the package, so this admits the consequence of an
    admission without admitting the package.  MEASURED: after normalising order and
    mtime, the only CONTENTS still differing were dev-lang/perl's and
    dev-python/charset-normalizer's — the two packages that own the five admitted
    files, differing in exactly those files' checksums.
    """
    line = CONTENTS_MTIME.sub("", line)
    if not line.startswith("obj "):
        return line
    rest, _, _md5 = line.rpartition(" ")
    if admitted(rest[4:].lstrip("/")):
        return rest
    return line


def _metadata_digest(path: str) -> str:
    """Digest of a vdb ``metadata`` file without the lines that record when."""
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        lines = [line for line in fh.read().splitlines()
                 if line.partition("=")[0] not in METADATA_VOLATILE]
    return hashlib.sha256("\n".join(lines).encode("utf-8", "surrogateescape")).hexdigest()


def _environment_digest(path: str) -> str:
    """Digest of a saved ebuild environment, decompressed, minus the volatile lines."""
    with bz2.open(path, "rt", encoding="utf-8", errors="surrogateescape") as fh:
        lines = [line for line in fh.read().splitlines()
                 if not line.startswith(ENVIRONMENT_VOLATILE)]
    return hashlib.sha256("\n".join(lines).encode(
        "utf-8", "surrogateescape")).hexdigest()


def _normalized_digest(path: str, basename: str) -> str:
    """Digest of a vdb record with its incidental ordering and clock removed."""
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        lines = fh.read().splitlines()
    if basename == "CONTENTS":
        lines = [_contents_line(line) for line in lines]
    return hashlib.sha256("\n".join(sorted(lines)).encode(
        "utf-8", "surrogateescape")).hexdigest()


def entries(root: str) -> Iterator[tuple[str, str]]:
    """(relative path, content key) for everything in the root, exclusions dropped.

    The key is the SYMLINK TARGET for a symlink, ``dir`` for a directory, and the
    sha256 for anything else — so a file that became a symlink, or a directory that
    became a file, is a difference and not a coincidence of equal digests.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(d for d in dirnames
                             if not excluded(os.path.join(rel_dir, d) if rel_dir else d))
        for name in dirnames:
            rel = os.path.join(rel_dir, name) if rel_dir else name
            yield rel, "dir"
        for name in sorted(filenames):
            rel = os.path.join(rel_dir, name) if rel_dir else name
            if excluded(rel):
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                yield rel, "sym " + os.readlink(path)
            elif rel.startswith(VDB_PREFIX) and name in VDB_UNORDERED:
                yield rel, _normalized_digest(path, name)
            elif rel.startswith(VDB_PREFIX) and name == VDB_METADATA:
                yield rel, _metadata_digest(path)
            elif rel.startswith(VDB_PREFIX) and name == VDB_ENVIRONMENT:
                yield rel, _environment_digest(path)
            else:
                yield rel, _digest(path)


def compare(a: str, b: str) -> tuple[list[str], list[str], list[str]]:
    """(only in a, only in b, differing) — each sorted."""
    ma, mb = dict(entries(a)), dict(entries(b))
    only_a = sorted(set(ma) - set(mb))
    only_b = sorted(set(mb) - set(ma))
    differ = sorted(k for k in set(ma) & set(mb) if ma[k] != mb[k])
    return only_a, only_b, differ


def _report(title: str, paths: list[str], limit: int = 60) -> None:
    print(f"portix: {len(paths)} {title}", file=sys.stderr)
    for path in paths[:limit]:
        print(f"  {path}", file=sys.stderr)
    if len(paths) > limit:
        print(f"  ... and {len(paths) - limit} more", file=sys.stderr)


USAGE: Final = "usage: diff.py [--report-only] <root-a> <root-b>"


def main(argv: list[str]) -> int:
    # --report-only is for a comparison that is EVIDENCE rather than a contract.
    # rebuild1 is transitional by construction -- merged into a root still filling --
    # so it is not supposed to equal rebuild2, and a permanently red gate would say
    # nothing.  MEASURED: 97 content differences rebuild1/rebuild2 against 0
    # rebuild2/rebuild3, which is what makes rebuild2 the shipped root.
    report_only = bool(argv) and argv[0] == "--report-only"
    if report_only:
        argv = argv[1:]
    if len(argv) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    a, b = argv
    only_a, only_b, differ = compare(a, b)

    # The known-upstream ones are REPORTED, never silently dropped.
    known = [(p, pattern) for p in differ if (pattern := admitted(p)) is not None]
    unknown = [p for p in differ if admitted(p) is None]
    if known:
        print(f"portix: {len(known)} paths differ because the PACKAGE is not "
              f"reproducible — admitted, and listed every run:")
        for path, pattern in known:
            print(f"  {path}\n      {KNOWN_UPSTREAM[pattern]}")
    missing_admissions = sorted(set(KNOWN_UPSTREAM) - {pattern for _, pattern in known})
    if missing_admissions:
        # An admission that stopped applying is an admission to delete, and finding
        # out by having it quietly cover nothing is how a list rots.
        print(f"portix: {len(missing_admissions)} admissions match nothing that differs "
              f"— delete them from KNOWN_UPSTREAM:")
        for path in missing_admissions:
            print(f"  {path}")

    if not (only_a or only_b or unknown):
        print(f"portix: the ladder converged — identical outside the named exclusions "
              f"and {len(known)} admitted upstream nondeterminisms")
        return 0
    if only_a:
        _report("paths only in the earlier root", only_a)
    if only_b:
        _report("paths only in the later root", only_b)
    if unknown:
        _report("paths whose content differs", unknown)
    if report_only:
        print("portix: reported, not gated — this comparison is evidence about a "
              "transitional rung, not a contract.")
        return 0
    print("portix: the ladder did NOT converge. Every one of these is either a defect "
          "to fix or an admission to justify in writing — never a reason to extend "
          "the ladder.", file=sys.stderr)
    return 1

