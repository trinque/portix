# portix

portix builds Nix derivations from unmodified Gentoo ebuilds by running real,
unmodified Portage inside the Nix sandbox. You name a package by its Portage atom;
Portage resolves the whole closure with its own resolver; each package in it becomes
one derivation, merged into a real Gentoo root and lifted out by Portage's own record
of what it installed.

It is for having Gentoo's supply chain and Nix's system management at once:

- **From Gentoo:** the ebuild tree is Gentoo's signed snapshot and the bootstrap seed
  is Gentoo's signed stage3, each verified against a pinned signing key before
  anything reads it. Every source tarball is fixed by the SHA512 in the signed tree's
  Manifest. Everything is compiled from source, by Gentoo's own toolchain, under
  Gentoo's own resolver and USE flags.
- **From Nix:** every build is hermetic and content-addressed, a closure is exactly
  what was declared, and a system that uses these packages is declarative, pinned,
  and rolled back like any other.

The seed is a binary, so it is discarded. portix rebuilds `@system` from it three
times. The root it ships is the second rebuild, which holds no store path and is
byte-identical to the third outside a few named upstream nondeterminisms. A package's
dependencies are built from their ebuilds too: nixpkgs supplies the sandbox and the
tools that unpack a root, and no compiled code.

## Requirements

- `x86_64-linux`. The seed is Gentoo's amd64 stage3.
- Nix with flakes and `allow-import-from-derivation`, which the `nix` command allows by
  default and Hydra does not. Resolution is Portage running in a derivation.
- To run what it builds, Linux 6.8 or newer and unprivileged user namespaces that may
  mount. Ubuntu's default AppArmor policy forbids that.
- Time and disk for the bootstrap: four rungs of an hour or more each, about 1.4 GB per
  rung in the store, before the first package. Each package's build then lays a copy
  of the root.

## Using it

```nix
{
  inputs.portix.url = "github:trinque/portix";

  outputs = { nixpkgs, portix, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      modules = [ ({ pkgs, ... }:
        let gentoo = portix.lib.mkPortix pkgs {
          makeConf.ACCEPT_KEYWORDS = "amd64 ~amd64";
          packageUse."www-client/chromium" = { wayland = true; };
        }; in {
          environment.systemPackages = [ (gentoo.emerge "app-misc/figlet") ];
        }) ];
    };
  };
}
```

Leave portix's `nixpkgs` unfollowed. The root is a function of the nixpkgs that lays
it, so following yours rebuilds the bootstrap every time you update.

**The tree is yours.** portix follows Gentoo's latest signed snapshot, so `nix flake
update` takes the newest; every snapshot is checked against Gentoo's pinned snapshot
key before anything reads it. To hold a dated one, override both inputs:

```nix
inputs.portix.inputs.gentooSnapshot.url = "file+https://distfiles.gentoo.org/snapshots/portage-20261006.tar.xz";
inputs.portix.inputs.gentooSnapshotSig.url = "file+https://distfiles.gentoo.org/snapshots/portage-20261006.tar.xz.gpgsig";
```

The seed is a dated stage3, `gentooStage3` and `gentooStage3Sig`, overridden the same
way: Gentoo names no latest tarball, only a pointer to one
(`releases/amd64/autobuilds/latest-stage3-amd64-openrc.txt`), and any recent amd64
openrc stage3 will do, since the bootstrap discards it. Portage follows its default
branch. A new tree, seed or Portage rebuilds the bootstrap and every package.

**`mkPortix pkgs config`** is portix under your make.conf policy. `pkgs` contributes
only its `config` (your licence policy). `config` takes Gentoo's own files by name,
and any other field is an error:

| field | is | example |
|---|---|---|
| `makeConf` | make.conf variables; `FEATURES` appends to portix's own | `{ ACCEPT_LICENSE = "@FREE"; CFLAGS = "-O2 -march=native"; }` |
| `packageUse` | package.use | `{ "dev-lisp/sbcl" = { source = true; }; }` |
| `packageAcceptKeywords` | package.accept_keywords | `{ "www-client/chromium" = "**"; }` |
| `packageEnv` | package.env, as variables | `{ "app-foo/bar" = { EXTRA_ECONF = "--x"; }; }` |

Your policy reaches the packages you build and never the root, which is built from
portix's defaults. Every policy therefore shares one root.

**`emerge "cat/pkg"`**, or `emerge { atom; select; mask; packageUse; }`, builds one
package. The other three fields apply to that package's resolution alone:

- `select`: atoms this closure chooses where an `|| ( … )` would otherwise take list
  order. sbcl's bootstrap Lisp is one.
- `mask`: what the resolver may not see. This is the only answer to a version left
  open: a `:*` slot operator, or `>=pkg-21` against a package versioned in
  `SLOT="0"`. Name the package the ambiguous atom reaches, not the one behind it.
- `packageUse`: package.use for this resolution only.

**`launcher { package; program; bind ? [ ]; name ? <basename> }`** runs one program
with extra host paths bound in. Use it when a program integrated with your machine
reads something outside the minimum, as chromium does with `/etc/chromium/policies`.

## What a package is

- `$out/bin/<program>`: a launcher for each program the package itself installs into
  `/usr/bin` or `/usr/sbin`. It runs the program with the root as `/` (see below).
- `$out/bin/portix-run <cmd>`: the same environment for anything else, such as an
  interpreter in the root running the package's module, or a program in `/opt/bin`.
- `$out/image`: the package's own files, exactly what its CONTENTS claims, plus its
  vdb entry.
- `$out/identity/{passwd,group,shadow,gshadow}`: the account lines it added.
- `$out/identity/dropped`: every owner and setuid/setgid request the store could not
  hold, as `kind`, `request`, `path` with the path image-relative. A store path
  carries no owner and no setuid bit; this record is what a consumer re-establishes
  them from, as NixOS's `security.wrappers` does for its own packages.
- `passthru.plan`: Portage's resolved plan, with every edge, version, USE flag and
  distfile.
- `meta.license`: Gentoo's LICENSE expression; `free` only if every licence in it is
  in Gentoo's `@FREE`, so nixpkgs' unfree gate applies.

A portix binary is linked against the root, so it has no store RPATH. Its launcher
builds a user and mount namespace and stacks the root, the package's runtime closure
and that closure's regenerated caches read-only with overlayfs. It binds back `/nix`,
`/home`, `/tmp`, `/run` and the kernel filesystems, then `pivot_root`s. The program
runs as you, against Gentoo's glibc, with Gentoo's `/etc`.

## Limits

- **File capabilities** (`fcaps.eclass`, 29 packages) are neither carried nor yet
  recorded: they are set in `pkg_postinst`, as xattrs, which a store path cannot
  hold.
- **Caches a base package owns:** a package that feeds a cache only a root package's
  `pkg_postinst` regenerates, such as the SGML catalogue, leaves that cache stale.
  Seven such postinsts read the long-gone build tree and cannot be re-run.
- **Reproducible on one builder, not yet across builders.** The build's job count and
  the build host's kernel release reach the root (SPEC §4); everything else is
  byte-identical between builds.
- **Trusting trust.** A self-reproducing payload in the seed's compiler would survive
  any number of rungs. Diverse double-compilation from an independent seed is the
  answer, and it is out of scope here, as it is for nixpkgs' own bootstrap.

## Working on portix

`nix flake check` runs the Python checks (mypy `--strict`, pytest) and evaluates every
suite derivation, which resolves every plan. `nix build .#hydraJobs.<name>` builds one
engine check, and building the first one builds the ladder. `python3 -m portix.why
why plan.json <cpv>` answers why a package is in a plan, `doubles` what is built twice,
and `diff` what a change did to the plan. SPEC.md is the design and AGENTS.md the
maintainer's brief.

## Licence

BSD 3-Clause (see LICENSE). The Gentoo tree, Portage, and
everything portix builds keep their own licences.
