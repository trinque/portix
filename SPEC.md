# portix — specification

This is the design: what portix is, why each part is what it is, and what was
measured to make it so. README.md says how to use it and AGENTS.md how to work on
it. The engine that preceded this one synthesized a root per package out of nixpkgs
parts. It and its seam catalogue are in git at `714294d7`, SPEC.md §3–9. Nothing
there describes portix now.

## 1. Thesis

A derivation is a pure value: a hermetic build whose identity is the hash of its
inputs. An ebuild is a script written against ambient state: USE flags, a live `/`,
the installed set. Building a derivation from an ebuild is closing over everything the
ebuild leaves implicit and freezing it. Nix is Gentoo with everything bound early.

portix keeps both rigours. Gentoo's supply chain: a signed tree, a signed seed,
every source fixed by a signed Manifest, everything compiled from source by Gentoo's
toolchain and resolved by Gentoo's resolver. Nix's system management: hermetic,
content-addressed builds, and systems that are declared, pinned and rolled back.

## 2. Principles

- **Real Portage, unmodified.** Eclasses, EAPI, metadata, resolution, the phases:
  Portage's, from its own source tree, with no patch. What the sandbox cannot grant
  is answered below Portage, at libc (§7), so every caller meets one rule.
- **A real root.** An ebuild is written against one filesystem where its
  dependencies are installed under `/usr` and `EPREFIX`, `BROOT` and `ESYSROOT` are
  all "". portix gives it that: Gentoo's own root, bootstrapped (§4).
- **Portage resolves; portix reads the answer.** Versions, `||` choices, slots, USE,
  cycle order and distfiles all come out of Portage's resolver. portix parses no atom
  and re-derives no dependency.
- **A second package source, not a name translator.** A dependency is built from its
  ebuild. Matching Gentoo atoms to nixpkgs attributes was the first engine's error.
- **By CONTENTS.** What a package is, is what Portage recorded it merged. Portage's
  bookkeeping is normalized when compared, never excluded.
- **Errors are information**, and **done means it runs**: every example is built and
  asserted in a derivation.

## 3. Trust

- The ebuild tree and the seed are flake inputs fetched as bytes and **verified to
  exist**. The only path to either is a derivation that checks its OpenPGP signature
  against a pinned key, with GOODSIG (not expired or revoked) and VALIDSIG's subkey
  and primary fingerprints. A good signature from any other key in Gentoo's keyring
  fails. The keyring is pinned too, so a rotation fails loudly.
- The tree is the snapshot, not git: it is signed, and it ships `metadata/md5-cache`.
  Which snapshot is the user's: Gentoo's latest by default, so `nix flake update`
  takes the newest, or any dated one. Verification makes the choice of source no
  choice of trust.
- Every distfile is fixed-output on the Manifest's SHA512. URLs list Gentoo's mirrors
  first. That costs no trust, since any URL must produce the recorded bytes, and it is
  needed: nix takes the first URL that downloads, and a forge-generated archive is
  re-compressed per request and rarely matches a Manifest.
- The seed is discarded structurally (§4). The shipped root holds no store reference,
  checked inside the build with compressed files read decompressed (§9).
- Convergence shows the bootstrap stopped moving (§4). It is not the proof of the
  discard. A constant carried by every rung is a fixed point too.
- Residue: a self-reproducing payload in the seed's compiler survives every rung.
  Diverse double-compilation from an independent seed reaches it, and is out of
  scope, as for nixpkgs' own bootstrap.

## 4. The bootstrap

| rung | `/` is | merges into | command |
|---|---|---|---|
| `stage1` | the seed | an empty `$out` | `emerge --root=$out --oneshot` over `profiles/default/linux/packages.build` |
| `rebuild1` | stage1 | `/`, in place | `emerge --emptytree --oneshot @system` |
| `rebuild2` | rebuild1 | `/`, in place | the same: **the shipped root** |
| `rebuild3` | rebuild2 | `/`, in place | the same, to be compared with rebuild2 |

- **stage1 is catalyst's.** With `--root=X` and SYSROOT unset, `ESYSROOT` is `/`
  (`LocationsManager.set_root_override`), so stage1 compiles against the seed and
  installs into the new root. Its `$out` starts empty, so no seed byte reaches it
  except through a merge. Of 147 planned packages, 144 merge into the new root and 3
  into the build host: a DEPEND the seed lacks is merged into the seed, inherent to
  `ROOT=X, SYSROOT=/`.
- **The merged-usr links are the stage builder's.** baselayout makes only the libdir
  links. Without `bin -> usr/bin` and its siblings, stage1 comes out split-usr and
  every later ebuild dies `env: 'bash': No such file`. stage1 lays them literally,
  copied from the seed's layout.
- **In place, not `--root`.** An ebuild's `[[ -z ${ROOT} ]]` branches, such as
  glibc's locale-gen and baselayout's shape, run only there.
- **rebuild2 is shipped, by measurement.** rebuild1 is merged into a root still
  filling, its early packages built by stage1's toolchain. The comparison measured 97
  content differences between rebuild1 and rebuild2 and none between rebuild2 and
  rebuild3.
- **Convergence came from removing two causes, not excluding them.** The first
  comparison found 809 differences. Portage probes the locale and sets `C.UTF-8` only
  once the probe can execute: 69 of 308 environments carried it in one rung against
  308 of 308 in the next, and `LC_COLLATE` reorders every sorted list. The locale is
  pinned to `C`, which needs no locale data. GNU `ar` stamps the wall clock into
  archives, which Gentoo leaves unfixed, so `--enable-deterministic-archives` is set
  through `package.env`. What remains, on the tree of 2026-10-07, is four files where
  perl's Configure stamps its configuration time, admitted in `diff.py` by path
  pattern with that cause, reported on every run, and reported again when an
  admission stops applying (a mypyc module's did, when mypy left `@system`). Line order
  in `NEEDED`/`CONTENTS` and the clock in Portage's records (mtimes, `BUILD_TIME`,
  `COUNTER`, and the `metadata` file Portage began writing in 2026-10) are normalized,
  not excluded. On the first run all 142 differing `NEEDED` files differed only in
  order, and two `CONTENTS` differed for real.
- **Convergence holds on one builder; across builders the root differs in what the
  build host leaks into it.** MEASURED between two rebuild2s of the 2026-07-30 tree,
  one built at four jobs on Linux 6.19 and one at two jobs on 6.18: the same 111,996
  paths, and under
  `diff.py`'s rules every byte identical except the job count (`NIX_BUILD_CORES` and
  Portage's own `zstd -T<jobs>` in each saved environment, python's
  `COMPILEALL_OPTS=-j<jobs>`) and the kernel release (perl's `osvers` in `Config.pm`
  and `Errno.pm`). The first was built by Portage with patches and the second without,
  so this is also the measure of what the patches did to the root: nothing. **Open:** a
  root reproducible across builders needs the job count held constant for the ladder,
  at the cost of its parallelism, and the kernel release kept out of perl's Configure.
- **One plan for the in-place rungs, resolved in the seed.** `--emptytree` plans as if
  nothing were installed, so the plan does not depend on which root resolves it (308
  packages and 423 distfiles either way, on the tree of 2026-07-30; 300 and 337 on
  that of 2026-10-07). It matters because the plan is
  import-from-derivation. Resolved in a rung's predecessor, merely evaluating a rung
  would build the ladder beneath it.
- **`--jobs=1`.** Portage queues deep system merges until no build runs, so with
  several jobs merges are ordered by build completion, which is wall-clock.
  `MAKEOPTS` carries the parallelism, set from nix's per-derivation `NIX_BUILD_CORES`
  in each build's environment. make.conf expansion never sees the environment.
- **What is emitted drops** the bind mountpoints, `PORTAGE_TMPDIR`, and Portage's and
  ldconfig's caches. mtimedb records the paths Portage was driven by, which is how a
  store path once reached a root.

## 5. Entering a root

`enterroot.py` unshares a user and mount namespace with our uid mapped to 0. emerge
refuses to merge otherwise, and the root's files must look root-owned. It binds the
tree, the distfiles and portix's config at their FHS paths, carries `/nix` and the
build directory at their own names, `pivot_root`s, and execs.

- A root is **unpacked or copied, never mounted from the store**. overlayfs takes a
  merged directory's mode and owner from its read-only, root-owned lower, so no upper
  can create an entry in one. Unpacking the seed takes 2.8 s for 2.8 GB.
- **One config root, bound at `/etc/portage`.** With `ROOT != "/"`, Portage builds
  the build host's config from the real `/etc/portage` and a thirteen-variable clean
  environment, ignoring `PORTAGE_CONFIGROOT`. Here the build host is the root being
  replaced.
- **The environment is an allowlist.** Portage saves each ebuild's environment
  compressed, invisible to nix's scanner, and filters only names it knows. An
  exported `PYTHONPATH`, then `CUDA_PATH` and seven more from an ordinary shell,
  reached every saved environment. Only `PATH`, `HOME`, `TERM`, `NIX_BUILD_CORES`,
  `SOURCE_DATE_EPOCH`, `ROOT` and `PORTAGE_*` cross. Store paths are passed as
  arguments.

## 6. Policy and resolution

- **The config surface is Gentoo's files by name**: `makeConf`, `packageUse`,
  `packageAcceptKeywords`, `packageEnv`. An unknown field is an error. The `FEATURES`
  base is mechanism and a caller's appends to it: nix already sandboxes, and
  Portage's sandboxes, userpriv and userfetch need a second uid that does not exist.
- **The root is built from portix's defaults only.** A caller's policy reaches the
  packages it builds, never the ladder, so every policy shares one root.
- **A package resolves under the caller's policy.** ACCEPT_KEYWORDS and
  ACCEPT_LICENSE decide which versions are visible at all.
- **The boundary is a synthetic installed database, not `package.provided`.** A
  package above the root resolves in the seed against vdb entries `plan.py` writes
  for every package of the root's plan, with SLOT and the rest from `aux_get` and the
  USE the root was built with. `package.provided` holds bare cpv strings, and
  `match_from_list` keeps a candidate whose slot it cannot read, so a provided
  docbook-xml-dtd-4.5 satisfied a dependency on slot 4.3 by name. Chromium's plan lost
  eight slot and subslot dependencies that way. The requested package's own entries
  are left out, or its atom would already be satisfied and Portage would plan nothing.
- **Two roots.** DEPEND and RDEPEND resolve against ROOT, BDEPEND against `/`. A
  declaration made in one is invisible to a question asked in the other.
- **A closure has no ambient, so choices are stated per target.** On Gentoo, which
  package answers `virtual/commonlisp` or `:*` is whatever the machine has.
  - `select` names the atoms a closure takes where a `||` would take list order. It
    makes them explicit goals and declares them installed in both roots, the rung of
    Portage's preference that beats list order. sbcl's
    `system-bootstrap? ( || ( >=dev-lisp/ecl-24.5.10 dev-lisp/clisp dev-lisp/sbcl ) )`
    takes ecl by default and clisp when selected. A mask cannot do this job: ecl is a
    BDEPEND, and masking it stops sbcl building.
  - `mask` is package.mask, the only answer to a version left ambient. `:*` and
    `>=llvmgold-21` against a `SLOT="0"` package versioned by major both resolve to
    the newest in the tree. That put a whole second LLVM toolchain into chromium's
    plan (175 packages against 163). Masking llvm 22 itself makes Portage refuse: it
    picks the newest candidate it can see and then insists on its dependencies. The
    mask must name what the ambiguous atom reaches. A USE flag cannot do it either:
    `llvm_slot_22` is not in IUSE where no `LLVM_COMPAT` exists, and Portage discards
    it silently.
  - `packageUse` for one target's resolution alone.
- **A node merges the USE its plan resolved.** The plan carries each package's whole
  IUSE, flag by flag. A node writes that as its own package.use line and is built
  under the instance policy minus USE. A USE change reaches nodes only through the
  plan, and the same package with the same USE in two closures is one derivation.

## 7. Identity

A store path carries no owner and no setuid or setgid bit. NAR records neither, and
nix rewrites modes to 0444/0555. The build sandbox refuses a setuid/setgid `chmod` with
EPERM from seccomp: measured, 4711 and 2711 fail while 1711 and 0711 succeed. The
build namespace maps one id. Requests for more come from many places: `fperms` and
`fowners` (125 packages request setuid/setgid, 152 a foreign owner), a build system's
own `chmod 4755` (util-linux's install hook, libtool's `install -m 4755`), Portage's
setgid directories, `fcaps` (29 packages).

`nosetid.c` is the one answer, at libc. It is LD_PRELOADed into every Portage process
and built inside the root by the root's gcc: a store-built `.so` would link another
libc and put a store path in `LD_PRELOAD`, which Portage carries into every ebuild
environment. Every chmod keeps its mode minus the setuid/setgid bits, every chown goes
to 0, and every request that lost something is appended to a log with its absolute
path. A request that cannot be recorded aborts.

After the merge, `emit.py` sorts the log against the package's own CONTENTS. A path,
read through the install image while staging (doubled slashes and all) and through
the root after the merge, that CONTENTS claims is the package's. That covers `fperms`
in `src_install` and a `chmod` in `pkg_postinst` alike. Everything else is the build
environment, such as the unpacked source and Portage's own directories: counted and
not shipped. The package's rows are `$out/identity/dropped`, image-relative, for a
consumer to re-establish outside the store, the way NixOS's `security.wrappers` does.

Open: **file capabilities.** `fcaps` sets them as an xattr in `pkg_postinst`. A NAR
holds no xattrs, and the record does not yet see them.

## 8. One derivation per package

A closure is one derivation per package of its plan. A node lays the root, copies in
the trees of everything it builds against, merges itself and emits itself. It names
only what it is built from, so the same package in two closures is one derivation, and
a dependency is referenced, never copied. Before this, glib existed twice in one
store, once inside each of two closures.

- **Cycles.** Gentoo's graphs have them, and Portage breaks each by choosing an
  order. A node builds against its dependencies transitively, along edges that point
  earlier in that order, or evaluation never ends.
- **Transitively,** because a dependency runs during the build and needs its own
  libraries: chez was copied in and lz4, chez's dependency, was not.
- **`--nodeps`.** Resolution happened once, for the closure. Re-resolving per node
  answers a question with no right answer in isolation: glib's node pulled
  shared-mime-info, which build-depends on glib.
- **Store trees are 0555,** so dependencies are untarred with
  `--delay-directory-restore` and their directories made writable as they land.
- **Account files are replayed.** An acct-user or acct-group package's whole effect
  is a mutation of `/etc/passwd` and friends, which baselayout owns: in nobody's
  CONTENTS. The files are append-only, so a node publishes the lines it added and a
  dependent replays them. acct-user/pdnsd died `useradd failed` without this.
- **`/etc/env.d` is folded.** A dependency's fragment arrives without `env-update`'s
  fold, and llvm's `LDPATH` only reaches the loader as a line of `ld.so.conf`. A node
  runs Portage's `env_update()` before it merges.
- **It is stricter than one merge into one root.** A package that under-declares
  against a sibling fails at compile. Under-declaring against the root does not: the
  root is under every node, as it is under every Gentoo build (acl-2.4.0 includes
  attr's header and does not depend on attr).

## 9. What a package is

- **Emitted by CONTENTS.** A diff of the root would also catch an `env-update`,
  another package's postinst, or a regenerated cache. The vdb entry rides along. A
  claimed file that is absent is reported, not fatal: glib's postinst unlinks the
  `giomodule.cache` it recorded. A fifo or device node is reported and not emitted.
- **Merged-usr is normalized through the root's own links.** `into /` records
  `/bin/getfacl`. Copied literally, a real `bin/` stacked over the root replaces its
  `bin` symlink, and `/bin/sh` vanishes.
- **`$out/bin` is the package's own programs** from its CONTENTS. Scanning gave
  chromium 221 launchers, `dbus-daemon` and `cupsd` among them, which on PATH shadow
  the system's. There is no `meta.mainProgram`: Gentoo records no such fact, and
  guessing from PN named `chromium` where the binary is `chromium-unstable`.
- **No store RPATH.** A portix binary links `/usr/lib64` in the root it was built in,
  and the launcher supplies that root.
- **Checks inside each rung:** every claimed path exists (a CONTENTS path may contain
  spaces, so it is delimited by what follows it), and no file names a store hash the
  build could see. Inside the sandbox `/nix/store` is exactly the rung's inputs, so
  `sys-apps/file`'s literal `#!/nix/store/` and a 2016 changelog's quoted store path
  are not references. Unclaimed paths are a census, not a fault: 195 on the stage1 of
  the 2026-10-07 tree, the work of `pkg_postinst` and `env-update` (compiler symlinks,
  the account files, `ld.so.conf`), as on any Gentoo root. A claim is read through
  the merged-usr links: Portage records `/sbin/agetty` for the file at
  `usr/bin/agetty`, and counted literally that was about as many again.

## 10. The launcher

- **`pivot_root`, never `chroot`.** The kernel refuses `CLONE_NEWUSER` to a chrooted
  process. Under a chroot, chromium fell back to its setuid helper, found
  `chrome-sandbox` at 0555, and aborted. `put_old` may be `new_root` itself, so the
  read-only merge needs nothing writable.
- **Layers:** the root, the runtime closure in merge order, and the closure's
  aggregate last. They are stacked read-only, one `lowerdir+` per layer, because
  `mount(2)`'s options are one page and chromium's 89 layers truncated mid-path.
  Scratch space is a private tmpfs inside the namespace, over a `mkdtemp` directory
  removed before exec.
- **Binds:** `/nix`, `/home`, `/tmp`, `/proc`, `/dev`, `/sys`, `/run`,
  `/etc/resolv.conf`, `/etc/static`, and whatever the caller names. `/etc` is the
  root's. NixOS's `/etc` is a symlink farm into `/etc/static`, so a caller-named
  `/etc` path dangles without it.
- **Environment.** The root's loader reads the root's `ld.so.cache`, regenerated by
  the aggregate, so the host's `LD_*` is dropped. `/etc/env.d` is folded by Portage's
  rules. An accumulating variable takes the root's value first and keeps the caller's
  after. A plain one the caller set is left alone. XDG's defaults apply where no
  fragment states them, because the caller's value masks the default GLib would
  otherwise use. TMPDIR is repaired, HOME never.
- The uid is not remapped: running a program is not a superuser action.

## 11. Composition

A file that is a join over the installed set is a value no package holds. A single
merge computes it for free. Assembled closures need an answer per class:

- **Folded from what is present:** `/etc/env.d`. A node and the aggregate run
  Portage's `env_update()`, which writes `ld.so.conf`, `ld.so.cache` and
  `profile.env`.
- **Published as a delta and replayed:** the account files, append-only (§8).
- **Regenerated by re-running Portage:** `gschemas.compiled`, `mime.cache`,
  `icon-theme.cache`. The aggregate is one derivation per closure. It lays the root
  and the closure, runs each stacked package's own `pkg_postinst` from its vdb entry
  in COUNTER order (gtk+'s needs pango present), then `env_update()`, and emits every
  path those created or rewrote. It keeps no table of caches, so it carries no
  per-ebuild knowledge. On chromium's closure, 38 postinsts produced 1,089 paths no
  CONTENTS claims. The root's own packages are not re-run: their postinsts already
  ran in place, and seven of them read `$T`, the long-gone build tree.
- **Lost:** a cache only a root package's postinst regenerates. A stacked SGML
  catalogue leaves `/etc/sgml/catalog` stale.

## 12. Invariants

- portix only reads the tree; the tree and the seed exist only through verification.
- Every distfile is fixed-output on its Manifest SHA512.
- Portage runs unmodified.
- nixpkgs contributes no compiled code to a package: only the sandbox, and the tools
  that unpack, copy and verify (`gnupg`, `gnutar`, `xz`, `python3`).
- The root holds no store reference; neither does any image.
- A node merges exactly its plan's USE, with `--nodeps`.
- Plan and merge resolve the same tree, under the same keywords and licences.
- A missing Manifest hash, an unknown mirror alias, a fetch-restricted package, an
  unknown config field and an unrecordable identity request each fail loudly.
