"""Unit tests for portix's pure parts: no namespace, no nix build.

Each case names the failure it pins; most of them happened once, in a build that took
hours to report it.
"""

from __future__ import annotations

import bz2
import gzip
import json
import lzma
import os
from pathlib import Path

import pytest

from portix import check, contents, emit
from portix.enterroot import parse_args, scrub_env
from portix.plan import distfile_urls, expand_mirror, mirror_subdir


def _file(root: Path, rel: str, data: bytes = b"x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _pkg(root: Path, cpv: str, lines: str) -> Path:
    d = root / "var/db/pkg" / cpv
    d.mkdir(parents=True)
    (d / "CONTENTS").write_text(lines)
    return d


# --- enterroot ---------------------------------------------------------------

def test_parse_args_splits_at_the_separator() -> None:
    assert parse_args(["/r", "/repo", "/dist", "/cfg", "/nix", "--", "/bin/bash", "-c", "x"]) \
        == ("/r", "/repo", "/dist", "/cfg", ["/nix"], ["/bin/bash", "-c", "x"])


def test_parse_args_rejects_malformed() -> None:
    for argv in (["/r", "/repo", "/dist", "/cfg", "/bin/true"],
                 ["/r", "/repo", "--", "/bin/true"],
                 ["/r", "/repo", "/dist", "/cfg", "--"]):
        with pytest.raises(ValueError):
            parse_args(argv)


def test_scrub_env_keeps_only_what_is_named() -> None:
    # An ordinary shell's environment once reached all 144 saved ebuild environments.
    kept = scrub_env({"PATH": "/usr/bin", "NIX_BUILD_CORES": "16", "ROOT": "/out",
                      "PORTAGE_BIN_PATH": "/nix/store/x-portage/bin",
                      "CUDA_PATH": "/nix/store/y-cuda", "out": "/nix/store/z"})
    assert set(kept) == {"PATH", "NIX_BUILD_CORES", "ROOT", "PORTAGE_BIN_PATH"}


# --- plan: distfile URLs ---------------------------------------------------------

def test_mirror_subdir_hashes_the_filename() -> None:
    assert mirror_subdir("perl-cleaner-2.31.tar.bz2") == "1a"


def test_expand_mirror() -> None:
    assert expand_mirror("https://x.invalid/a.tgz", {}) == ["https://x.invalid/a.tgz"]
    assert expand_mirror("mirror://cpan/id/x.tgz", {"cpan": ["https://a/", "https://b"]}) \
        == ["https://a/id/x.tgz", "https://b/id/x.tgz"]
    with pytest.raises(SystemExit):
        expand_mirror("mirror://nosuch/x.tgz", {})


def test_distfile_urls_put_gentoos_mirrors_first() -> None:
    assert distfile_urls("perl-cleaner-2.31.tar.bz2", ["mirror://cpan/x.tar.bz2"],
                         ["http://d.invalid/"], {"cpan": ["https://c.invalid"]}) == [
        "http://d.invalid/distfiles/1a/perl-cleaner-2.31.tar.bz2",
        "https://c.invalid/x.tar.bz2"]


# --- contents ----------------------------------------------------------------

def test_contents_paths_may_contain_spaces() -> None:
    # line.split()[1] truncated 14 real paths and called them a half-finished merge.
    assert contents.parse("obj /usr/share/Help/Borland Makefiles.rst abc 1781704386") \
        == contents.Entry("obj", "/usr/share/Help/Borland Makefiles.rst", "")
    assert contents.parse("sym /usr/bin/my tool -> ../lib/my tool.real 1781704386") \
        == contents.Entry("sym", "/usr/bin/my tool", "../lib/my tool.real")
    assert contents.parse("fif /run/initctl") == contents.Entry("fif", "/run/initctl", "")


def test_contents_refuses_an_unparseable_line(tmp_path: Path) -> None:
    path = _pkg(tmp_path, "sys-apps/which-2.23", "obj /usr/bin/which\n") / "CONTENTS"
    with pytest.raises(SystemExit):
        contents.read(path.as_posix())


# --- check -------------------------------------------------------------------

BASELAYOUT = "dir /var\ndir /var/db\n"


def test_unowned_and_missing(tmp_path: Path) -> None:
    _file(tmp_path, "usr/bin/which")
    _file(tmp_path, "usr/bin/smuggled")
    _file(tmp_path, "var/log/emerge.log")          # Portage's own; never walked
    _pkg(tmp_path, "sys-apps/which-2.23", BASELAYOUT +
         "dir /usr\ndir /usr/bin\nobj /usr/bin/which a 1\nobj /usr/bin/gone b 1\n")
    claimed = check.contents_entries(tmp_path.as_posix())
    assert check.unowned(tmp_path.as_posix(), claimed) == ["usr/bin/smuggled"]
    assert check.missing(tmp_path.as_posix(), claimed) == [("usr/bin/gone", "sys-apps/which-2.23")]


def test_unowned_reads_a_claim_through_the_merged_usr_links(tmp_path: Path) -> None:
    # Portage records /sbin/agetty for the file at usr/bin/agetty; counted literally,
    # that was 196 of stage1's 393 "unclaimed" paths.
    _file(tmp_path, "usr/bin/agetty")
    (tmp_path / "sbin").symlink_to("usr/bin")
    _pkg(tmp_path, "sys-apps/util-linux-2.41", BASELAYOUT +
         "dir /usr\ndir /usr/bin\nobj /sbin/agetty a 1700000000\n")
    claimed = check.contents_entries(tmp_path.as_posix())
    assert check.unowned(tmp_path.as_posix(), claimed) == ["sbin"]   # the link itself


def test_missing_accepts_a_symlink_that_resolves_only_as_root(tmp_path: Path) -> None:
    (tmp_path / "usr/bin").mkdir(parents=True)
    (tmp_path / "usr/bin/gcc").symlink_to("/usr/x86_64-pc-linux-gnu/gcc-bin/15/gcc")
    _pkg(tmp_path, "sys-devel/gcc-config-2.12", "sym /usr/bin/gcc -> /usr/x86_64-pc-linux-gnu/gcc-bin/15/gcc 1\n")
    assert check.missing(tmp_path.as_posix(), check.contents_entries(tmp_path.as_posix())) == []


def _real_hash() -> str:
    for name in sorted(os.listdir("/nix/store")):
        head = name.split("-", 1)[0]
        if len(head) == 32 and name != head:
            return head
    raise AssertionError("no store path to borrow a hash from")


def test_store_paths_reads_through_any_compression(tmp_path: Path) -> None:
    ref = f"X=/nix/store/{_real_hash()}-cuda\n".encode()
    _file(tmp_path, "var/db/pkg/x/y-1/environment.bz2", bz2.compress(ref))
    _file(tmp_path, "usr/share/doc/a.gz", gzip.compress(ref))
    _file(tmp_path, "usr/share/doc/b.xz", lzma.compress(ref))
    _file(tmp_path, "usr/share/doc/plain", b"nothing to see")
    # A gzip magic number on bytes that are not gzip is scanned as the bytes it is.
    _file(tmp_path, "usr/share/doc/c.bin", b"\x1f\x8b not gzip " + ref)
    assert check.store_paths(tmp_path.as_posix()) == [
        "usr/share/doc/a.gz", "usr/share/doc/b.xz", "usr/share/doc/c.bin",
        "var/db/pkg/x/y-1/environment.bz2"]


def test_store_paths_ignores_what_is_not_a_reference(tmp_path: Path) -> None:
    # A 2016 changelog quoting a store path that names nothing here, and file's magic
    # database rule for execline scripts.
    _file(tmp_path, "ChangeLog", b"/nix/store/nyj6xd7s1n1w8c0xdwk5ddhi7bjcyi9x-bash/bin/bash")
    _file(tmp_path, "magic", b"0\tstring\t#!/nix/store/\n")
    assert check.store_paths(tmp_path.as_posix()) == []


def test_store_paths_ignores_the_root_itself(tmp_path: Path) -> None:
    own = "0gnz7yq85nk2rc27pkinl68y0h35z6pk"
    root = tmp_path / f"{own}-portix-stage-1"
    _file(root, "var/cache/edb/mtimedb", f"/nix/store/{own}-portix-stage-1/x".encode())
    assert check.own_hash(root.as_posix()) == own.encode()
    assert check.store_paths(root.as_posix()) == []


# --- diff --------------------------------------------------------------------

def test_diff_ignores_volatiles_and_line_order(tmp_path: Path) -> None:
    from portix.diff import compare
    a, b = tmp_path / "a", tmp_path / "b"
    for root, when, needed in ((a, b"1700000000", "x y\nz w\n"), (b, b"1800000000", "z w\nx y\n")):
        _file(root, "usr/bin/which", b"same")
        _file(root, "var/db/pkg/x/y-1/BUILD_TIME", when)
        _file(root, "var/db/pkg/x/y-1/NEEDED", needed.encode())
        _file(root, "var/db/pkg/x/y-1/CONTENTS", b"obj /usr/bin/which aaa " + when + b"\n")
        _file(root, "var/log/emerge.log", when)
    assert compare(a.as_posix(), b.as_posix()) == ([], [], [])
    _file(b, "var/db/pkg/x/y-1/CONTENTS", b"obj /usr/bin/which bbb 1\n")   # a real change
    assert compare(a.as_posix(), b.as_posix())[2] == ["var/db/pkg/x/y-1/CONTENTS"]


def test_diff_sees_a_changed_file_and_symlink(tmp_path: Path) -> None:
    from portix.diff import compare
    a, b = tmp_path / "a", tmp_path / "b"
    _file(a, "usr/bin/which", b"one")
    _file(b, "usr/bin/which", b"two")
    (a / "bin").symlink_to("usr/bin")
    (b / "bin").symlink_to("usr/sbin")
    _file(a, "gone")
    assert compare(a.as_posix(), b.as_posix()) == (["gone"], [], ["bin", "usr/bin/which"])


def test_diff_admits_an_upstream_files_checksum_only(tmp_path: Path) -> None:
    from portix.diff import KNOWN_UPSTREAM, compare
    admitted = next(iter(KNOWN_UPSTREAM))
    a, b = tmp_path / "a", tmp_path / "b"
    for root, md5 in ((a, "aaa"), (b, "bbb")):
        _file(root, "var/db/pkg/x/y-1/CONTENTS",
              f"obj /{admitted} {md5} 1700000000\nobj /usr/bin/other ccc 1700000000\n".encode())
    assert compare(a.as_posix(), b.as_posix())[2] == []
    _file(b, "var/db/pkg/x/y-1/CONTENTS",
          f"obj /{admitted} bbb 1700000000\nobj /usr/bin/other ddd 1700000000\n".encode())
    assert compare(a.as_posix(), b.as_posix())[2] == ["var/db/pkg/x/y-1/CONTENTS"]


def test_diff_admissions_hold_across_versions() -> None:
    # The tree moves; a perl or python bump must not turn an admission into a failure.
    from portix.diff import admitted
    assert admitted("usr/lib64/perl5/5.44/x86_64-linux/CORE/config.h") is not None
    assert admitted("usr/lib64/libperl.so") is None


def test_diff_reads_the_metadata_file_without_its_clock(tmp_path: Path) -> None:
    # Portage from 2026-10 consolidates an entry's keys into `metadata`; BUILD_TIME,
    # COUNTER and #dir_mtime there made all 300 entries differ between two rungs.
    from portix.diff import compare
    a, b = tmp_path / "a", tmp_path / "b"
    for root, when, counter in ((a, "1791402189", "657"), (b, "1791410041", "957")):
        _file(root, "var/db/pkg/x/y-1/metadata", (f"#format=1\nBUILD_TIME={when}\n"
              f"COUNTER={counter}\nSLOT=0\nUSE=amd64\n#dir_mtime={when}471257584\n").encode())
    assert compare(a.as_posix(), b.as_posix()) == ([], [], [])
    _file(b, "var/db/pkg/x/y-1/metadata", b"#format=1\nBUILD_TIME=1\nSLOT=0\nUSE=amd64 X\n")
    assert compare(a.as_posix(), b.as_posix())[2] == ["var/db/pkg/x/y-1/metadata"]


def test_diff_drops_a_loop_order_dependent_environment_line(tmp_path: Path) -> None:
    from portix.diff import compare
    a, b = tmp_path / "a", tmp_path / "b"
    for root, arr in ((a, "([0]=$'x')"), (b, "()")):
        _file(root, "var/db/pkg/dev-libs/json-c-0.18/environment.bz2",
              bz2.compress(f'declare -x CFLAGS="-O2"\ndeclare -a mod_lines={arr}\n'.encode()))
    assert compare(a.as_posix(), b.as_posix()) == ([], [], [])


# --- emit --------------------------------------------------------------------

def _merged_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for rel, data in (("usr/bin/figlet", b"ELF"), ("usr/share/figlet/f.flf", b"font"),
                      ("usr/bin/somebody-elses", b"no")):
        _file(root, rel, data)
    (root / "usr/bin/fig").symlink_to("figlet")
    (root / "bin").symlink_to("usr/bin")
    for name in emit.IDENTITY:
        _file(root, f"etc/{name}", b"root:x:0:0\n" + (b"locate:x:400:\n" if name == "group" else b""))
        _file(tmp_path / "base", name, b"root:x:0:0\n")
    _pkg(root, "app-misc/figlet-2.2.5", "dir /usr/bin\nobj /usr/bin/figlet a 1\n"
         "sym /usr/bin/fig -> figlet 1\nobj /usr/share/figlet/f.flf b 1\n"
         "obj /bin/chkfont c 1\nobj /usr/lib/gone d 1\nfif /run/figlet.fifo\n")
    _file(root, "usr/bin/chkfont", b"ELF")
    return root


def test_emit_lifts_exactly_what_contents_claims(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, out = _merged_root(tmp_path), tmp_path / "out"
    log = _file(tmp_path, "nosetid.log",
                b"mode\t2711\t/var/tmp/portage/app-misc/figlet-2.2.5/image//usr/bin/figlet\n"
                b"mode\t4755\t/bin/chkfont\n"
                b"owner\t250:250\t/var/tmp/portage/app-misc/figlet-2.2.5/work\n"
                b"mode\t2770\t/var/cache/edb/dep\n")
    assert emit.main([root.as_posix(), out.as_posix(), "app-misc/figlet-2.2.5",
                      log.as_posix(), (tmp_path / "base").as_posix()]) == 0
    image = out / "image"
    assert (image / "usr/bin/figlet").read_bytes() == b"ELF"
    assert (image / "usr/bin/fig").is_symlink()
    assert not (image / "usr/bin/somebody-elses").exists()   # the whole point of CONTENTS
    # `into /` records /bin/chkfont; a real bin/ would replace the root's symlink.
    assert (image / "usr/bin/chkfont").exists() and not (image / "bin").exists()
    assert not (image / "run").exists()                      # a fifo is not content
    assert (image / "var/db/pkg/app-misc/figlet-2.2.5/CONTENTS").exists()
    # NUL-TERMINATED: `read -d ""` drops a final field that has no delimiter.
    assert (out / "launchers").read_bytes() == b"usr/bin/chkfont\0usr/bin/fig\0usr/bin/figlet\0"
    assert (out / "identity/group").read_text() == "locate:x:400:\n"
    assert (out / "identity/passwd").read_text() == ""
    # Staged (doubled slash and all) and post-merge requests on claimed paths are the
    # package's; the build tree and Portage's cache are not.
    assert (out / "identity/dropped").read_text() == \
        "mode\t2711\tusr/bin/figlet\nmode\t4755\tusr/bin/chkfont\n"
    printed = capsys.readouterr().out
    assert "absent after the merge — /usr/lib/gone" in printed
    assert "2 identity requests recorded, 2 in the build environment" in printed


def test_emit_fails_loudly_when_the_package_is_not_merged(tmp_path: Path) -> None:
    root = _merged_root(tmp_path)
    with pytest.raises(FileNotFoundError):
        emit.main([root.as_posix(), (tmp_path / "out").as_posix(), "app-misc/nope-1",
                   "/nonexistent", (tmp_path / "base").as_posix()])


# --- aggregate ---------------------------------------------------------------

def test_aggregate_emits_what_was_created_or_rewritten(tmp_path: Path) -> None:
    from portix import aggregate
    root, out = tmp_path / "root", tmp_path / "out"
    _file(root, "etc/ld.so.cache", b"old")
    _file(root, "usr/share/untouched", b"same")
    before = aggregate.snapshot(root.as_posix(), "build")
    tmp = _file(root, "etc/ld.so.cache.tmp", b"new")
    os.replace(tmp, root / "etc/ld.so.cache")     # written aside, renamed over
    _file(root, "usr/share/mime/mime.cache", b"fold")
    _file(root, "etc/machine-id", b"minted")
    _file(root, "build/root/junk", b"the carried build directory")
    after = aggregate.snapshot(root.as_posix(), "build")
    assert aggregate.emit_delta(root.as_posix(), before, after, out.as_posix()) == (2, 0, 1)
    assert (out / "etc/ld.so.cache").read_bytes() == b"new"
    assert (out / "usr/share/mime/mime.cache").exists()
    assert not (out / "usr/share/untouched").exists() and not (out / "etc/machine-id").exists()


# --- launch: /etc/env.d and the loader -----------------------------------------

def _envd(tmp_path: Path, files: dict[str, str]) -> str:
    d = tmp_path / "env.d"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(body)
    return d.as_posix()


def test_envd_fold_follows_portage(tmp_path: Path) -> None:
    from portix.launch import envd_aggregate
    env, accumulating = envd_aggregate(_envd(tmp_path, {
        "05binutils": "MANPATH=/a\n", "binutils": "junk\n",          # no digits: eselect's
        "30xdg": 'XDG_DATA_DIRS="/usr/local/share"\nCOLON_SEPARATED="XDG_DATA_DIRS"\n',
        "50baselayout": 'PATH="/usr/local/bin:/usr/bin"\nLESS="-R -M --shift 5"\n',
        "60llvm": 'PATH="/usr/lib/llvm/21/bin"\n',
        "70dup": 'PATH="/usr/bin"\nLESS=-R\n',
        "90xdg": 'XDG_DATA_DIRS="/usr/share"\n'}))
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/usr/lib/llvm/21/bin"
    assert env["XDG_DATA_DIRS"] == "/usr/local/share:/usr/share"
    assert env["LESS"] == "-R"                                      # plain: last wins
    assert "XDG_DATA_DIRS" in accumulating and "LESS" not in accumulating
    assert "COLON_SEPARATED" not in env


def test_repair_env_drops_the_hosts_loader_and_puts_the_root_first(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from portix.launch import repair_env
    envd = _envd(tmp_path, {"50baselayout": 'PATH="/usr/bin"\nLDPATH="/usr/lib64"\nEDITOR=nano\n'})
    for var, value in (("PATH", "/run/current-system/sw/bin"), ("LD_PRELOAD", "/nix/store/x.so"),
                       ("LD_LIBRARY_PATH", "/nix/store/y/lib"), ("EDITOR", "vim"),
                       ("XDG_DATA_DIRS", "/run/current-system/sw/share"), ("TMPDIR", "/nonexistent/tmp")):
        monkeypatch.setenv(var, value)
    repair_env(envd)
    assert os.environ["PATH"] == "/usr/bin:/run/current-system/sw/bin"
    assert "LD_PRELOAD" not in os.environ and "LD_LIBRARY_PATH" not in os.environ
    assert "LDPATH" not in os.environ                 # the loader reads ld.so.cache
    assert os.environ["EDITOR"] == "vim"              # the caller's statement
    assert os.environ["XDG_DATA_DIRS"] == "/usr/local/share:/usr/share:/run/current-system/sw/share"
    assert os.environ["TMPDIR"] == "/tmp"


# --- census --------------------------------------------------------------------

def test_census_reads_a_log_with_nul_bytes(tmp_path: Path) -> None:
    # grep skips a binary file SILENTLY, which read as "nothing happened" for a run
    # where 51 nodes had built and 5 had failed.
    from portix.census import census, read
    log = _file(tmp_path, "build.log",
                "x> portix: emitted www-client/chromium-151.0 — 15 files, 2 symlinks, "
                "3 directories\n".encode() + b"\x00\x00ninja\x00\n"
                b"error: Cannot build '/nix/store/" + b"a" * 32 + b"-libclc-21.1.8.drv'.\n")
    c = census(read(log.as_posix()))
    assert c.nodes == ["www-client/chromium-151.0"] and c.failed == ["libclc-21.1.8"]
    assert not c.ok


def test_census_verdicts(tmp_path: Path) -> None:
    from portix.census import RUNNING, census, main
    assert not census("nothing happened here\n").ok              # zero work is not a pass
    started = "".join(f"building '/nix/store/{'a' * 31}{i}-p{i}.drv'...\n" for i in range(2))
    assert census("these 160 derivations will be built:\n" + started).running
    assert census("these 2 derivations will be built:\n" + started).ok
    failed = census("these 160 derivations will be built:\n" + started +
                    "error: builder for '/nix/store/" + "b" * 32 + "-flac-1.5.0.drv' failed "
                    "to produce output path for output 'out'\n"
                    "error: Cannot build '/nix/store/" + "c" * 32 + "-lua-5.4.drv'.\n"
                    "       Reason: 1 dependency failed.\n"
                    "error: interrupted by the user\n")
    assert not failed.running and not failed.ok          # --keep-going leaves work unentered
    assert failed.root_failures == ["flac-1.5.0"] and failed.cascade == ["lua-5.4"]
    assert failed.unattributed == ["error: interrupted by the user"]
    log = _file(tmp_path, "b.log", ("these 9 derivations will be built:\n" + started).encode())
    assert main([log.as_posix()]) == RUNNING


# --- why -------------------------------------------------------------------------

def _plan(tmp_path: Path) -> str:
    """chromium's second-LLVM shape: three atoms reaching llvm-22, and the real cycle
    between llvm-22 and its toolchain-symlinks."""
    def p(cpv: str, deps: list[str]) -> dict[str, object]:
        cat, rest = cpv.split("/", 1)
        pname, _, version = rest.rpartition("-")
        return {"cpv": cpv, "category": cat, "pname": pname, "version": version,
                "deps": deps, "rdeps": [], "root": "/"}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"packages": [
        p("www-client/chromium-151.0", ["dev-util/bindgen-0.72.1", "llvm-core/llvm-21.1.8"]),
        p("dev-util/bindgen-0.72.1", ["llvm-core/clang-22.1.8"]),
        p("llvm-core/clang-22.1.8", ["llvm-core/llvm-22.1.8"]),
        p("llvm-core/llvm-21.1.8", ["llvm-core/llvmgold-22"]),
        p("llvm-core/llvmgold-22", ["llvm-core/llvm-22.1.8"]),
        p("llvm-core/llvm-22.1.8", ["llvm-core/llvm-toolchain-symlinks-22"]),
        p("llvm-core/llvm-toolchain-symlinks-22", ["llvm-core/llvm-22.1.8"]),
    ]}))
    return path.as_posix()


def test_why_names_every_parent_and_a_route_from_the_target(tmp_path: Path) -> None:
    from portix.why import doubles, load, matches, route, why
    plan = load(_plan(tmp_path))
    out = why(plan, "llvm-core/llvm-22")
    assert "dep   of llvm-core/clang-22.1.8" in out and "dep   of llvm-core/llvmgold-22" in out
    path = route(plan, "llvm-core/llvm-22.1.8")
    assert path is not None and path[0].parent == "www-client/chromium-151.0"
    assert matches(plan, "llvm-core/llvm-22") == ["llvm-core/llvm-22.1.8"]
    assert "llvm-core/llvm  21.1.8 22.1.8" in doubles(plan)
