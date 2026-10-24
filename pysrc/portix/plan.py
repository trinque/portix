"""Resolve a merge plan with Portage's own resolver, and name every distfile it needs.

Runs inside a root (``enterroot.py``), against that root's installed database.
Prints one JSON object::

    {"packages":  [{cpv, category, pname, pf, version, root, use, useflags,
                    deps, rdeps, distfiles, license, description, homepage}, ...],
     "distfiles": [{name, size, sha512, urls}, ...],
     "requested": {atom: [cpv, ...]}}

``useflags`` is the package's whole IUSE, each flag on or off as resolved, so a node
can merge with exactly the USE it was planned with.  ``requested`` names what each
requested atom resolved to.

Distfiles come from here because the sandbox has no network: Portage's
``getFetchMap`` already resolves SRC_URI (USE conditionals, ``->`` renames) and the
Manifest carries the hash Gentoo signed.  Gentoo's mirrors are listed first.  Every
distfile is fixed-output on that SHA512, so the order costs no trust; it matters
because nix takes the first URL that downloads, not the first that matches, and a
forge-generated archive is re-compressed per request and rarely matches a Manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Final

#: Gentoo's mirror layout (``distfiles/layout.conf``: ``filename-hash BLAKE2B 8``).
MIRROR_HASH_BYTES: Final = 1

#: What a synthetic vdb entry carries, read through ``aux_get``.  SLOT is the point.
VDB_KEYS: Final = (
    "SLOT", "EAPI", "KEYWORDS", "IUSE", "RDEPEND", "DEPEND", "BDEPEND", "PDEPEND",
    "LICENSE", "DESCRIPTION", "HOMEPAGE", "PROPERTIES", "RESTRICT", "REQUIRED_USE",
    "INHERITED",
)


def mirror_subdir(filename: str) -> str:
    return hashlib.blake2b(filename.encode()).hexdigest()[: 2 * MIRROR_HASH_BYTES]


def expand_mirror(uri: str, thirdparty: dict[str, list[str]]) -> list[str]:
    """``mirror://alias/path`` -> every URL the alias names.  An unknown alias is fatal:
    dropping it would surface much later, as a fetch that ran out of mirrors."""
    if not uri.startswith("mirror://"):
        return [uri]
    alias, _, path = uri[len("mirror://"):].partition("/")
    if alias not in thirdparty:
        raise SystemExit(f"portix plan: unknown mirror alias '{alias}' in {uri}")
    return [m.rstrip("/") + "/" + path for m in thirdparty[alias]]


def distfile_urls(filename: str, ebuild_uris: list[str], gentoo_mirrors: list[str],
                  thirdparty: dict[str, list[str]]) -> list[str]:
    sub = mirror_subdir(filename)
    urls = [f"{m.rstrip('/')}/distfiles/{sub}/{filename}" for m in gentoo_mirrors]
    for uri in ebuild_uris:
        urls.extend(expand_mirror(uri, thirdparty))
    return urls


def write_installed(portdb: Any, root: str, installed: list[dict[str, str]]) -> None:
    """Declare packages installed in ``root``'s vdb, one entry per cpv.

    Not ``package.provided``: its entries are bare cpv strings, and ``match_from_list``
    keeps a candidate whose slot it cannot read, so a provided docbook-xml-dtd-4.5
    satisfies a dependency on slot 4.3 by name.  A vdb entry has a SLOT file.  USE is
    the caller's -- what the shipped root was built with -- because ``glib[introspection]``
    is answered from it.
    """
    from portage.versions import catpkgsplit

    for counter, entry in enumerate(installed, start=1):
        cpv = entry["cpv"]
        split = catpkgsplit(cpv)
        if split is None:
            raise SystemExit(f"portix plan: cannot split cpv {cpv!r}")
        category, pname, version, revision = split
        pf = f"{pname}-{version}" + ("" if revision == "r0" else f"-{revision}")
        values = dict(zip(VDB_KEYS, portdb.aux_get(cpv, list(VDB_KEYS))))
        values.update(CATEGORY=category, PF=pf, USE=entry["use"], COUNTER=str(counter),
                      repository=portdb.aux_get(cpv, ["repository"])[0], CONTENTS="")
        entry_dir = os.path.join(root, "var/db/pkg", category, pf)
        os.makedirs(entry_dir, exist_ok=True)
        for key, value in values.items():
            with open(os.path.join(entry_dir, key), "w", encoding="utf-8") as fh:
                # Empty is an empty FILE: a lone newline in CONTENTS is a line Portage
                # tries, and fails, to parse.
                fh.write(value + "\n" if value else "")


def _not_runtime(priority: Any) -> bool:
    """``child_nodes`` takes what to IGNORE; ``runtime_slot_op`` (``zlib:0/1=``) counts."""
    return not (getattr(priority, "runtime", False)
                or getattr(priority, "runtime_post", False)
                or getattr(priority, "runtime_slot_op", False))


def plan(root: str, atoms: list[str], emptytree: bool, provided: list[dict[str, str]],
         select: list[str]) -> dict[str, Any]:
    # ROOT before the import: portage.settings is built at import time, and
    # `emerge --root` is itself only a write to os.environ.
    os.environ["ROOT"] = root
    sys.path.insert(0, os.environ["PORTAGE_PYM_PATH"])

    import portage
    from portage.dep import Atom, match_from_list, use_reduce
    from portage.manifest import Manifest
    from portage.versions import catpkgsplit, cpv_getkey
    from _emerge.actions import load_emerge_config
    from _emerge.create_depgraph_params import create_depgraph_params
    from _emerge.depgraph import backtrack_depgraph
    from portage._sets import load_default_config

    portdb = portage.portdb
    # A requested package is never declared installed, or its atom would already be
    # satisfied and Portage would plan nothing.
    requested = {a: Atom(a) for a in atoms if not a.startswith("@")}
    keys = {a.cp for a in requested.values()}
    installed = [p for p in provided if cpv_getkey(p["cpv"]) not in keys]

    # SELECT: the atoms this closure chooses where an `|| ( ... )` would otherwise be
    # answered by list order.  Declared installed -- the rung of Portage's preference
    # that beats list order -- in both roots, because DEPEND and RDEPEND resolve
    # against ROOT and BDEPEND against "/"; and resolved as atoms too, so they are
    # fetched and merged like anything else.
    chosen = []
    for atom in sorted(set(select)):
        cpv = portdb.xmatch("bestmatch-visible", atom)
        if not cpv:
            raise SystemExit(f"portix plan: selected {atom} has no visible version")
        chosen.append({"cpv": str(cpv), "use": ""})
    if installed or chosen:
        write_installed(portdb, root, installed + chosen)
    if chosen:
        write_installed(portdb, "/", chosen)

    opts: dict[str, Any] = {"--pretend": True, "--oneshot": True, "--quiet": True,
                            "--root": root}
    if emptytree:
        opts["--emptytree"] = True
    goals = atoms + sorted(set(select))
    cfg = load_emerge_config(action=None, args=goals, opts=opts)
    for eroot in cfg.trees:
        root_config = cfg.trees[eroot].get("root_config")
        if root_config is not None:
            set_config = load_default_config(root_config.settings, cfg.trees[eroot])
            root_config.setconfig = set_config
            root_config.sets = set_config.getSets()

    settings = cfg.target_config.settings
    ok, depgraph, _ = backtrack_depgraph(
        settings, cfg.trees, opts, create_depgraph_params(opts, ""), "", goals, None)
    if not ok:
        depgraph.display_problems()
        raise SystemExit(f"portix plan: Portage could not resolve {goals}")

    nodes = [n for n in depgraph.altlist()
             if hasattr(n, "cpv") and getattr(n, "operation", None) == "merge"]
    graph = depgraph._dynamic_config.digraph
    gentoo_mirrors = settings.get("GENTOO_MIRRORS", "").split()
    thirdparty = settings.thirdpartymirrors()

    packages: list[dict[str, Any]] = []
    distfiles: dict[str, dict[str, Any]] = {}
    for node in nodes:
        cpv = str(node.cpv)
        split = catpkgsplit(cpv)
        if split is None:
            raise SystemExit(f"portix plan: cannot split cpv {cpv!r}")
        category, pname, version, revision = split
        pvr = version if revision == "r0" else f"{version}-{revision}"
        use = frozenset(node.use.enabled)
        restrict, license_, description, homepage = portdb.aux_get(
            cpv, ["RESTRICT", "LICENSE", "DESCRIPTION", "HOMEPAGE"])
        if "fetch" in use_reduce(restrict, uselist=use, flat=True):
            raise SystemExit(f"portix plan: {cpv} is RESTRICT=fetch, so its distfiles "
                             "cannot be fetched by anyone but you")

        fetchmap = portdb.getFetchMap(cpv, useflags=use)
        packages.append({
            "cpv": cpv, "category": category, "pname": pname, "pf": f"{pname}-{pvr}",
            "version": pvr, "root": str(node.root),
            "use": " ".join(sorted(use)),
            "useflags": " ".join(f if f in use else f"-{f}" for f in sorted(node.iuse.all)),
            # The resolved graph's edges: only the `||` branch resolution chose, only
            # the USE conditionals that are on.  Runtime edges decide what a launcher
            # stacks; every edge decides what a node is built against.
            "rdeps": sorted({str(c.cpv) for c in graph.child_nodes(
                node, ignore_priority=_not_runtime) if hasattr(c, "cpv")}),
            "deps": sorted({str(c.cpv) for c in graph.child_nodes(node)
                            if hasattr(c, "cpv")}),
            "distfiles": sorted(fetchmap),
            "license": license_, "description": description, "homepage": homepage,
        })

        digests = Manifest(os.path.join(portdb.getRepositoryPath(node.repo), category,
                                        pname), settings["DISTDIR"]).fhashdict.get("DIST", {})
        for filename, uris in fetchmap.items():
            if filename in distfiles:
                continue
            digest = digests.get(filename)
            if digest is None or "SHA512" not in digest:
                raise SystemExit(f"portix plan: no Manifest SHA512 for '{filename}' ({cpv})")
            distfiles[filename] = {
                "name": filename, "size": int(digest["size"]), "sha512": digest["SHA512"],
                "urls": distfile_urls(filename, list(uris), gentoo_mirrors, thirdparty),
            }

    return {"packages": packages,
            "distfiles": [distfiles[k] for k in sorted(distfiles)],
            "requested": {a: sorted({p["cpv"] for p in packages
                                     if match_from_list(atom, [p["cpv"]])})
                          for a, atom in requested.items()}}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="plan.py")
    parser.add_argument("--emptytree", action="store_true")
    parser.add_argument("--provided", help="JSON list of {cpv, use} declared installed")
    parser.add_argument("--select", help="JSON list of atoms this closure selects")
    parser.add_argument("root")
    parser.add_argument("atoms", nargs="+")
    args = parser.parse_args(argv)

    def load(path: str | None) -> Any:
        if path is None:
            return []
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    result = plan(args.root, args.atoms, args.emptytree, load(args.provided),
                  load(args.select))
    json.dump(result, sys.stdout, indent=1, sort_keys=True)
    return 0
