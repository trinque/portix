"""Portage's CONTENTS: what a merge says it installed.

A path may contain spaces, so it is delimited by what follows it -- ``obj`` ends with
an md5 and an mtime, ``sym`` with `` -> target mtime`` -- the grammar of Portage's own
``_contents_re`` (``vartree.py``).  An unparseable line is fatal: a claim that cannot
be read cannot be checked.
"""

from __future__ import annotations

import os
import re
from typing import Final, NamedTuple

LINE: Final = re.compile(
    r"^(?:"
    r"(?P<kind>dir|dev|fif) (?P<path>.+)"
    r"|obj (?P<obj>.+) \S+ \d+"
    r"|sym (?P<sym>.+) -> (?P<target>.+) (?:\d+|\(\d+, .*\))"
    r")$"
)


class Entry(NamedTuple):
    kind: str
    #: Absolute, as Portage recorded it.
    path: str
    #: A symlink's target; empty for anything else.
    target: str


def parse(line: str) -> Entry:
    m = LINE.match(line)
    if m is None:
        raise ValueError(f"unparseable CONTENTS line: {line!r}")
    if m.group("obj") is not None:
        return Entry("obj", m.group("obj"), "")
    if m.group("sym") is not None:
        return Entry("sym", m.group("sym"), m.group("target"))
    return Entry(m.group("kind"), m.group("path"), "")


def read(path: str) -> list[Entry]:
    entries: list[Entry] = []
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        for number, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                entries.append(parse(line))
            except ValueError as exc:
                raise SystemExit(f"portix: {path} line {number}: {exc}")
    return entries


def bridges(root: str) -> dict[str, str]:
    """The root's merged-usr links, read from the root: ``bin -> usr/bin`` gives
    ``{"bin": "usr/bin"}``.  An ebuild installing ``into /`` makes Portage record
    ``/bin/getfacl`` for the file at ``usr/bin/getfacl``."""
    out: dict[str, str] = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.islink(path) and os.path.isdir(path):
            target = os.readlink(path)
            if not target.startswith("/"):
                out[name] = target.rstrip("/")
    return out


def normalize(rel: str, bridge: dict[str, str]) -> str:
    """A root-relative claim, read through the merged-usr links."""
    head, sep, tail = rel.partition("/")
    return bridge[head] + sep + tail if head in bridge else rel
