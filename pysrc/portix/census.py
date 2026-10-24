"""Read a portix build log and say what happened.

WHY THIS IS NOT A GREP PIPELINE.  A portix build log carries NUL bytes -- ninja and
Portage both emit them, and so does anything that draws a progress bar -- and grep
treats such a file as binary and skips it SILENTLY.  MEASURED: two chromium runs were
reported as "nothing happened" for exactly that reason, when fifty-one nodes had built
and five had failed.  A census that cannot tell "no failures" from "I did not look" is
worse than no census, because it reads as good news.

So the log is read as BYTES and decoded with errors="replace".  There is no encoding a
build can emit that makes this quietly return nothing.

The counts are the point, not the absence of failures.  ``nodes`` being zero is itself
a finding -- it means the run died before building anything, which is what a
failure-only filter shows as silence.
"""

from __future__ import annotations

import re
import sys
from typing import Final, NamedTuple

#: One emitted node: portix's own line, which names the package and what it lifted.
#: ONLY PRESENT UNDER `nix build -L`. Without it nix keeps a successful builder's
#: output in the store and prints none of it, so a census that counted nothing else
#: would call a finished build empty. Hence the two counters below, which come from
#: nix's own progress reporting and are always there.
EMITTED: Final = re.compile(
    r"portix: emitted (?P<atom>\S+) — (?P<files>\d+) files, (?P<links>\d+) symlinks, "
    r"(?P<dirs>\d+) directories")

#: "these 160 derivations will be built:" — how much work nix decided there was.
ANNOUNCED: Final = re.compile(r"^(?:these (?P<n>\d+) derivations|this derivation) "
                              r"will be built:", re.MULTILINE)

#: One derivation entered. Nix prints this when it STARTS, so during a run it is ahead
#: of what has finished, and at the end it equals what was built.
STARTED: Final = re.compile(r"^building '/nix/store/[a-z0-9]{32}-(?P<drv>[^']+)\.drv'",
                            re.MULTILINE)

#: ANY error line naming a derivation, not one enumerated message. MEASURED, and this is
#: the third time this file was blind to a real failure: matching only
#: "error: Cannot build '<drv>'" missed
#: "error: builder for '<drv>' failed to produce output path for output 'out'", which is
#: what nix says when a builder exits 0 having created nothing -- the exact shape of a
#: broken heredoc swallowing the rest of a build script. Enumerating nix's phrasings is
#: the losing move; a .drv inside a line that starts with `error:` is a failure whatever
#: the wording.
FAILED: Final = re.compile(
    r"^error:[^\n]*'/nix/store/[a-z0-9]{32}-(?P<drv>[^']+)\.drv'", re.MULTILINE)

#: A derivation that failed only because something it needs did. Separated because
#: "7 failed" reads as seven problems when it is two problems and five consequences.
CASCADE: Final = re.compile(
    r"^error: Cannot build '/nix/store/[a-z0-9]{32}-(?P<drv>[^']+)\.drv'\.\n"
    r"\s*Reason: \d+ dependenc(?:y|ies) failed", re.MULTILINE)

#: An `error:` line that names no derivation at all, counted so that a failure this file
#: cannot attribute still moves the number rather than vanishing.
UNATTRIBUTED: Final = re.compile(r"^error: (?![^\n]*/nix/store/)[^\n]*", re.MULTILINE)

#: An evaluation error, which produces NO failure line at all and is therefore the
#: exact case a failure-only filter misses.
EVAL_ERROR: Final = re.compile(r"^       error: (?P<message>.+)$", re.MULTILINE)

#: Failure signatures worth naming, because each says a different thing about the
#: engine rather than about the package.
CAUSES: Final = (
    ("out of memory", re.compile(r"Killed signal terminated program|virtual memory exhausted")),
    ("missing shared library", re.compile(r"error while loading shared libraries: (\S+)")),
    ("identity file", re.compile(r"useradd failed|groupadd failed")),
    ("read-only dependency tree", re.compile(r"Cannot mkdir: Permission denied|"
                                             r"Cannot open: Permission denied")),
    ("claimed and absent", re.compile(r"claimed and absent after the merge")),
    ("check measured nothing", re.compile(r"nothing was checked")),
)


class Census(NamedTuple):
    nodes: list[str]
    failed: list[str]
    eval_errors: list[str]
    causes: dict[str, int]
    #: How many derivations nix said it would build, and how many it entered.
    announced: int
    started: list[str]
    #: Failures that are only consequences of another failure, and error lines this
    #: file could not pin to a derivation.
    cascade: list[str]
    unattributed: list[str]

    @property
    def root_failures(self) -> list[str]:
        """The failures that are problems rather than consequences of one."""
        return [d for d in self.failed if d not in set(self.cascade)]

    @property
    def ok(self) -> bool:
        """A pass needs work done AND nothing failed. Zero of both is not a pass."""
        return ((bool(self.nodes) or bool(self.started))
                and not self.failed and not self.eval_errors and not self.unattributed)

    @property
    def running(self) -> bool:
        """Work entered, work outstanding, and NOTHING FAILED.

        The "0 built, 0 failed" log is BOTH a run that never started and a run three
        minutes in, and calling the second one "did not start" is the NUL-byte grep's
        lie pointing the other way.  So the counters say which.

        But an unentered derivation does NOT mean a run is still going: under
        `--keep-going` nix finishes with every derivation downstream of a failure never
        entered, so a FINISHED, FAILED run also has started < announced.  MEASURED, and
        shipped wrong for two hours: chromium reported "running — none failed so far"
        directly beneath a list of seven failures.  A failure decides it.

        The residue, named rather than papered over: a run that is genuinely still going
        AND has already lost a derivation under `--keep-going` reads as FAIL.  That is
        true — it did fail — and it is the safe direction to be wrong in.
        """
        return (bool(self.started) and len(self.started) < self.announced
                and not self.failed and not self.eval_errors and not self.unattributed)


def read(path: str) -> str:
    """Bytes, decoded permissively. The one thing this must never do is skip the file."""
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", errors="replace")


def census(text: str) -> Census:
    nodes = [m.group("atom") for m in EMITTED.finditer(text)]
    failed = sorted({m.group("drv") for m in FAILED.finditer(text)})
    eval_errors = sorted({m.group("message").strip()
                          for m in EVAL_ERROR.finditer(text)})
    causes = {}
    for name, pattern in CAUSES:
        hits = len(pattern.findall(text))
        if hits:
            causes[name] = hits
    announced = sum(int(m.group("n")) if m.group("n") else 1
                    for m in ANNOUNCED.finditer(text))
    started = [m.group("drv") for m in STARTED.finditer(text)]
    cascade = sorted({m.group("drv") for m in CASCADE.finditer(text)})
    unattributed = sorted({m.group(0).strip() for m in UNATTRIBUTED.finditer(text)})
    return Census(nodes, failed, eval_errors, causes, announced, started,
                  cascade, unattributed)


def report(c: Census) -> str:
    lines = [f"derivations: {len(c.started)} entered of {c.announced} announced",
             f"nodes built: {len(c.nodes)}"
             + ("" if c.nodes else "   (portix's own emit line needs `nix build -L`)")]
    if c.causes:
        lines.append("causes seen:")
        lines += [f"  {n:28s} {k}" for n, k in sorted(c.causes.items(),
                                                      key=lambda kv: -kv[1])]
    if c.eval_errors:
        lines.append(f"evaluation errors: {len(c.eval_errors)}")
        lines += [f"  {m[:110]}" for m in c.eval_errors[:5]]
    if c.failed:
        roots = c.root_failures
        lines.append(f"failed derivations: {len(roots)} "
                     f"({len(c.cascade)} more failed only because a dependency did)")
        lines += [f"  {d}" for d in roots]
    if c.unattributed:
        lines.append(f"errors naming no derivation: {len(c.unattributed)}")
        lines += [f"  {m[:110]}" for m in c.unattributed[:5]]
    if c.running:
        lines.append(f"VERDICT: running — {c.announced - len(c.started)} derivations "
                     "not yet entered, none failed so far")
        return "\n".join(lines)
    lines.append("VERDICT: " + ("pass" if c.ok else "FAIL"))
    if not c.started and not c.nodes and not c.failed and not c.eval_errors:
        lines.append("  (nothing entered, nothing built, nothing failed — "
                     "no run reached this log)")
    return "\n".join(lines)


USAGE: Final = "usage: census.py <build.log>    # exit 0 pass, 1 FAIL, 2 usage, 3 running"

#: A THIRD exit state, because a caller that treats 0 as "done and good" would
#: otherwise read a half-finished run as a finished one — which is the whole failure
#: mode this tool exists to close.
RUNNING: Final = 3


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    c = census(read(argv[0]))
    print(report(c))
    if c.running:
        return RUNNING
    return 0 if c.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised by the monitors
    sys.exit(main(sys.argv[1:]))
