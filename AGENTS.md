# portix: the maintainer's brief

portix builds Nix derivations from unmodified Gentoo ebuilds with real, unmodified
Portage, in a real Gentoo root bootstrapped from Gentoo's signed stage3. README.md is
for users and SPEC.md is the design. **Read SPEC.md before changing anything.** When
you learn something new about where Gentoo and Nix disagree, it goes there, once.

## Layout

- `flake.nix`: the pinned inputs, the verification that makes the tree and the seed
  exist, `lib.mkPortix`, and the outputs (`checks` = Python; `hydraJobs` = the
  suite).
- `stage.nix`: the engine. Policy and config, the ladder, plans, nodes, aggregates,
  `emerge`, `launcher`.
- `suite.nix`: the engine checks, each an example built and asserted.
- `nosetid.c`: the identity shim (SPEC §7).
- `pysrc/portix/`: `enterroot` (into a root), `plan` (Portage's resolver), `emit` (a
  package out of a root), `contents` (CONTENTS' grammar), `check` (a rung's checks),
  `diff` (convergence), `aggregate` (the regenerated folds), `launch` (running a
  program). Also two tools: `why` (`python3 -m portix.why {why,doubles,diff}
  plan.json`; get a plan with `nix eval --json .#<pkg>.plan`) and `census` (read a
  build log; see Traps).

## What a change costs

A derivation's identity is every byte it interpolates, comments inside `''` strings
included. Price a change before making it, by evaluating
`nix eval --raw path:.#packages.x86_64-linux.rebuild2.drvPath` before and after.
Never reason the price from a list.

- **The ladder (hours per rung):** the rung scripts, `layRoot`, `portageEnv`,
  `noSetid`, `mkConfig` and `rootConfig`, `nosetid.c`, `enterroot.py`, `check.py`,
  `contents.py`, the plan's output, and every input pin (tree, stage3, Portage,
  nixpkgs).
- **The world (every package):** the node script, `nodeConfig`, `emit.py`, and
  everything in the ladder.
- **One closure:** `aggregate.py`, a plan's config.
- **Seconds:** `launch.py`, the launchers, `diff.py`, `why.py`, `census.py`, docs, and
  nix comments outside `''` strings.

Python is referenced per module (`py` in stage.nix), so a derivation depends on
exactly the modules it imports. Keep it that way.

**Finish the changes, then build.** Land everything in dependency order and start one
build. Do not start an hours-long run and then find the next edit.

## Updating the tree

`nix flake update` takes Gentoo's latest snapshot and Portage's default branch. The
seed is dated: read `releases/amd64/autobuilds/latest-stage3-amd64-openrc.txt` for the
current one and change both stage3 URLs. Any of the three rebuilds the ladder and the
world. The suite names no exact versions and `diff.py`'s admissions are patterns, so
an update tests the engine rather than the pins; when an admission matches nothing,
delete it. A key rotation fails verification on purpose: confirm the new key out of
band, then pin its fingerprints.

## Traps

- **A comment inside a nix `''` string is string content.** `${VAR}` is
  interpolated (write `''${VAR}`). Two single quotes close the string. Nix strips
  the minimum indentation of the whole string, so one line at column 0 stops
  stripping everywhere, and a heredoc terminator that is no longer at column 0
  swallows the rest of the script. The derivation then "fails to produce output
  path" and names no file here.
- **Verify the generated script, not just that it evaluated:** `nix derivation show
  <drv>`, and check that each heredoc closes at column 0.
- **A quiet build is not a green one.** `python3 -m portix.census <log>` reads the log
  as bytes: grep skips a file with NUL bytes silently and once reported "nothing
  happened" for a run where 51 nodes built and 5 failed. Zero built is a fail.
- zsh does not word-split `nix build $ATTRS`; use `xargs -a`.
- Prefix commands with `export PATH=/run/current-system/sw/bin:$PATH` when this
  environment starts without coreutils.

## Discipline

Errors are information: no `|| true`, no `2>/dev/null`, no silenced failure. A fix is
general, a property of where Gentoo and Nix meet, never a special case for one ebuild.
If you find yourself writing one, you have found a seam: name it in SPEC.md. When a
seam keeps spawning per-case work, the frame is wrong. Do the taxonomy first.

## Publishing

portix is published on its own as `github:trinque/portix`, always as squashed history:
each publication is one commit whose tree is this directory as committed and whose
parent is the previous publication. The monorepo's commits, their messages and every
earlier version of a file never leave it. From the monorepo root:

    git fetch git@github.com:trinque/portix.git main
    c=$(git commit-tree HEAD:portix -p FETCH_HEAD -m "<what changed since the last publication>")
    git push git@github.com:trinque/portix.git "$c:refs/heads/main"

The message is all a reader of portix sees of its history. A push that does not build
on what is published is refused, so a stale fetch fails the publication rather than
replacing one.
