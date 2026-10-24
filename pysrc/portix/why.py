"""Ask a plan why a package is in it, and what a config change did to it.

A plan is Portage's RESOLVED graph, not a wish list: every edge in it is a decision the
resolver already made.  So the question a configuration actually raises -- "what pulled
llvm 22 into a browser that pins 21" -- is answerable exactly, by walking those edges
backwards, and NOT answerable by reading an ebuild, because an ebuild says
`dev-util/spirv-llvm-translator:*` and the plan says which one that turned out to be.

MEASURED, and it is why this exists: chromium's plan carried a complete second LLVM
toolchain.  Three ebuilds put it there, each through a different kind of atom, and none
of them mentions "22" anywhere.  Reading the plan backwards named all three in one pass;
reading ebuilds had already failed to find them, and had produced three `package.use`
lines that moved not one package.

Three questions, because those are the three that came up:

  why      -- every resolved edge into a package, and one path to it from the target
  doubles  -- what this closure builds at two versions, which is the general form
  diff     -- what changed between two plans, which is how a mask is verified

Input is the JSON `plan.py` emits, which is also what nix hands back:

    nix eval --json <flake>#<pkg>.plan > plan.json
    python3 -m portix.why why plan.json llvm-core/llvm-22

A plan names a package once per ROOT it is merged into -- DEPEND/RDEPEND resolve against
ROOT and BDEPEND against `/` -- so the same cpv legitimately appears twice.  The walk
keeps the root on each edge, because "this is a BUILD dependency of that" is usually the
answer to why something is present at all.
"""

from __future__ import annotations

import json
import sys
from typing import Final, NamedTuple


class Edge(NamedTuple):
    """One resolved dependency: `parent` needs `child`, in `root`, as `kind`."""

    parent: str
    child: str
    root: str
    kind: str


class Plan(NamedTuple):
    cpvs: list[str]
    #: `cat/pname` -> the versions of it this plan carries.  Kept apart from `cpvs`
    #: because "one package at two versions" is a question about the package, not the
    #: cpv, and it is the GENERAL form of the second-LLVM bug.
    versions: dict[str, list[str]]
    edges: list[Edge]

    @property
    def parents(self) -> dict[str, list[Edge]]:
        by: dict[str, list[Edge]] = {}
        for e in self.edges:
            by.setdefault(e.child, []).append(e)
        return by


def load(path: str) -> Plan:
    with open(path, "rb") as fh:
        raw = json.loads(fh.read().decode("utf-8"))
    packages = raw["packages"]
    cpvs = [str(q["cpv"]) for q in packages]
    versions: dict[str, list[str]] = {}
    for q in packages:
        seen = versions.setdefault(f'{q["category"]}/{q["pname"]}', [])
        if q["version"] not in seen:
            seen.append(str(q["version"]))
    edges = [
        Edge(str(q["cpv"]), str(child), str(q["root"]), kind)
        for q in packages
        for kind in ("deps", "rdeps")
        for child in q[kind]
    ]
    return Plan(cpvs, versions, edges)


def matches(plan: Plan, query: str) -> list[str]:
    """Exact cpv if there is one, else every cpv the query is a prefix of.

    A prefix, not a substring: `llvm-core/llvm-22` should find `llvm-core/llvm-22.1.8`
    and must NOT also find `llvm-core/llvm-toolchain-symlinks-22`, which is a different
    package that merely ends in the same digits.
    """
    seen = dict.fromkeys(plan.cpvs)
    if query in seen:
        return [query]
    return [c for c in seen if c.startswith(query)]


def origins(plan: Plan) -> list[str]:
    """The packages nothing in the plan depends on: the target, and the caller's atoms.

    Not "whatever the resolver listed last" -- the plan is a graph and this reads its
    sources off the edges, so it stays right when a `select` adds a second one.
    """
    children = {e.child for e in plan.edges}
    return [c for c in dict.fromkeys(plan.cpvs) if c not in children]


def route(plan: Plan, target: str) -> list[Edge] | None:
    """The shortest chain of resolved edges from an origin to `target`.

    Walked FORWARD from the origins rather than backward from the target, and that is
    the whole reason it can be trusted: a dependency graph has cycles -- llvm-22 and
    llvm-toolchain-symlinks-22 name each other -- so walking backward stops at whichever
    cycle member it happened to reach first and reports it as a source.  Forward, every
    chain starts somewhere that really is one.
    """
    kids: dict[str, list[Edge]] = {}
    for e in plan.edges:
        kids.setdefault(e.parent, []).append(e)
    seen = set(origins(plan))
    frontier: list[tuple[str, list[Edge]]] = [(o, []) for o in origins(plan)]
    while frontier:
        node, path = frontier.pop(0)
        if node == target and path:
            return path
        for e in kids.get(node, []):
            if e.child in seen:
                continue
            seen.add(e.child)
            frontier.append((e.child, [*path, e]))
    return None


def why(plan: Plan, query: str) -> str:
    """Two exact answers, and no inference between them.

    WHAT DEPENDS ON IT is the primitive that cannot lie: one line per resolved edge,
    read straight off the plan.  Iterating it by hand is how chromium's second LLVM was
    found, and each of the three ebuilds that put it there showed up as a direct parent
    of something -- `spirv? ( dev-util/spirv-llvm-translator:* )`, `llvm-core/clang:*`,
    `>=llvm-core/llvmgold-${LLVM_MAJOR}`.  A single chain would have named ONE of them
    and read as the answer.
    """
    found = matches(plan, query)
    if not found:
        return f"{query}: not in this plan ({len(set(plan.cpvs))} packages)"
    lines: list[str] = []
    for cpv in found:
        lines.append(cpv)
        ups = plan.parents.get(cpv, [])
        if not ups:
            lines.append("  nothing depends on it — it is the target, or an atom the "
                         "caller named")
        for e in sorted(set(ups)):
            lines.append(f"  {e.kind[:-1]:5s} of {e.parent}  [root {e.root}]")
        path = route(plan, cpv)
        if path:
            lines.append("  from the target: " + " -> ".join(
                [path[0].parent] + [x.child for x in path]))
    return "\n".join(lines)


def doubles(plan: Plan) -> str:
    """Every package this closure builds at more than one version.

    A REPORT and not an assertion, because some of them are right: `docbook-xml-dtd`
    is SLOT="4.3" and SLOT="4.4" and a closure needing both really does need both.
    What it catches is the other kind, where two versions of one package means the
    closure compiles the same thing twice because an ebuild declined to say which it
    wanted -- llvm 21 and llvm 22, in a browser that pins 21.
    """
    dupes = {k: v for k, v in plan.versions.items() if len(v) > 1}
    if not dupes:
        return f"{len(plan.versions)} packages, none at more than one version"
    lines = [f"{len(dupes)} of {len(plan.versions)} packages are at more than one version"]
    for name in sorted(dupes):
        lines.append(f"  {name}  {' '.join(sorted(dupes[name]))}")
    return "\n".join(lines)


def diff(before: Plan, after: Plan) -> str:
    a, b = set(before.cpvs), set(after.cpvs)
    lines = [f"before: {len(a)} packages    after: {len(b)} packages"]
    for sign, which in (("-", a - b), ("+", b - a)):
        for cpv in sorted(which):
            lines.append(f"  {sign} {cpv}")
    if a == b:
        lines.append("  (identical package sets)")
    return "\n".join(lines)


USAGE: Final = (
    "usage: why.py why     <plan.json> <cpv-or-prefix>\n"
    "       why.py doubles <plan.json>\n"
    "       why.py diff    <before.json> <after.json>")

#: subcommand -> how many arguments follow it.
COMMANDS: Final = {"why": 2, "doubles": 1, "diff": 2}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS or len(argv) != COMMANDS[argv[0]] + 1:
        print(USAGE, file=sys.stderr)
        return 2
    if argv[0] == "why":
        print(why(load(argv[1]), argv[2]))
    elif argv[0] == "doubles":
        print(doubles(load(argv[1])))
    else:
        print(diff(load(argv[1]), load(argv[2])))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand against real plans
    sys.exit(main(sys.argv[1:]))
