# portix testing itself: every example built AND asserted in a derivation, so "it
# compiled" is never mistaken for "it works". The examples test the ENGINE -- what
# portix does to any package -- not eclasses: the ladder already merges 308 packages'
# worth of those, and every engine bug of the 2026-08 rework passed fifty-four eclass
# checks green.
{ pkgs, portix }:

let
  inherit (pkgs) lib;
  # Test policy, not a recommendation: several examples are ~amd64, and the licence
  # gate is nixpkgs' to apply here, not Portage's.
  inherit (portix { makeConf = { ACCEPT_KEYWORDS = "amd64 ~amd64"; ACCEPT_LICENSE = "*"; }; }) emerge;

  packages = {
    figlet = emerge "app-misc/figlet";
    wget = emerge "net-misc/wget";
    acl = emerge "sys-apps/acl";
    cffi = emerge "dev-python/cffi";
    list-moreutils = emerge "dev-perl/List-MoreUtils";
    wiki2beamer = emerge "app-text/wiki2beamer";
    inih = emerge "dev-libs/inih";
    cronlocker = emerge "app-admin/cronlocker";
    bubblewrap = emerge "sys-apps/bubblewrap";
    shared-mime-info = emerge "x11-misc/shared-mime-info";
    mlocate = emerge "sys-apps/mlocate";
    upx-bin = emerge "app-arch/upx-bin";
    abduco = emerge "app-misc/abduco";
    dejavu = emerge "media-fonts/dejavu";
    # Plans only, never built: each pair differs in one knob. The mask is whatever
    # version the unmasked plan chose, so the pair holds whatever the tree carries.
    acl-masked = emerge { atom = "sys-apps/acl"; mask = [ ">=${aclCpv}" ]; };
    sbcl-bootstrap = emerge { atom = "dev-lisp/sbcl"; packageUse."dev-lisp/sbcl".system-bootstrap = true; };
    sbcl-bootstrap-clisp = emerge {
      atom = "dev-lisp/sbcl";
      packageUse."dev-lisp/sbcl".system-bootstrap = true;
      select = [ "dev-lisp/clisp" ];
    };
  };
  p = packages;

  test = name: tools: script: pkgs.runCommand "portix-test-${name}" { nativeBuildInputs = tools; } ''
    set -euo pipefail
    ${script}
    touch $out
  '';
  fail = msg: "echo ${lib.escapeShellArg msg} >&2; exit 1";
  # The right soname is NEEDed and nothing names the store: a portix binary resolves
  # inside the root its launcher supplies, never through an RPATH.
  links = bin: soname: ''
    patchelf --print-needed ${bin} | grep -q ${soname} || { ${fail "${bin} does not NEED ${soname}"}; }
    if patchelf --print-rpath ${bin} | grep -q /nix/store; then ${fail "${bin} has a store RPATH"}; fi
  '';
  plan = pkg: map (q: q.cpv) pkg.plan.packages;
  aclCpv = lib.head packages.acl.plan.requested."sys-apps/acl";
  has = re: cpvs: lib.any (c: builtins.match re c != null) cpvs;

  tests = {
    # A program runs through its launcher and finds data at the path baked into it.
    figlet = test "figlet" [ ] ''
      ${p.figlet}/bin/figlet portix | grep -q '_'
    '';
    # $out/bin is the package's OWN programs, every one: the list is NUL-terminated
    # because `read -d ""` drops a last field that is merely separated.
    launcher-set = test "launcher-set" [ ] ''
      [ "$(ls ${p.figlet}/bin | tr '\n' ' ')" = "chkfont figlet figlist portix-run showfigfonts " ] \
        || { ${fail "figlet's bin is not exactly its four programs and portix-run"}; }
    '';
    # A closure of portix-built nodes, linked the root's way.
    wget = test "wget" [ pkgs.patchelf ] ''
      ${p.wget}/bin/wget --version | grep -q 'GNU Wget'
      ${links "${p.wget}/image/usr/bin/wget" "libssl"}
    '';
    # acl installs `into /`, so Portage records /bin/getfacl: emitted literally, a real
    # bin/ would replace the root's bin -> usr/bin and /bin/sh would vanish. And acl
    # 2.4.0 includes attr's header without depending on attr: the root is under every
    # node, as it is under every Gentoo build.
    acl = test "acl" [ pkgs.patchelf ] ''
      [ ! -e ${p.acl}/image/bin ] || { ${fail "acl's image has a real bin/"}; }
      ${p.acl}/bin/getfacl --version | grep -q getfacl
      ${links "${p.acl}/image/usr/bin/getfacl" "libacl"}
    '';
    # A python C extension and its runtime python dependency, each its own node,
    # stacked: a live FFI call through a portix-built libffi.
    cffi = test "cffi" [ ] ''
      [ "$(${p.cffi}/bin/portix-run python3 -c 'import cffi; f=cffi.FFI(); f.cdef("int strlen(const char*);"); print(f.dlopen(None).strlen(b"portix"))')" = 6 ]
    '';
    # A perl XS module whose chain spans several nodes.
    list-moreutils = test "list-moreutils" [ ] ''
      [ "$(${p.list-moreutils}/bin/portix-run perl -MList::MoreUtils=uniq -e 'print join(",",uniq(1,1,2,3,3))')" = "1,2,3" ]
    '';
    # A launcher that is a symlink into the root's python-exec, dispatching to the
    # package's own script.
    wiki2beamer = test "wiki2beamer" [ ] ''
      mkdir -p /tmp/w2b
      printf '==== A slide\n* hello\n' > /tmp/w2b/in.txt
      ${p.wiki2beamer}/bin/wiki2beamer /tmp/w2b/in.txt > /tmp/w2b/out.tex
      grep -q '\\begin{itemize}' /tmp/w2b/out.tex
    '';
    # Developing against a portix library: pkg-config and gcc inside the stacked root.
    inih = test "inih" [ ] ''
      mkdir -p /tmp/inih
      printf '%s\n' '#include <ini.h>' 'static int h(void*u,const char*s,const char*n,const char*v){return 1;}' \
        'int main(){return ini_parse("/nonexistent",h,0)==-1?0:1;}' > /tmp/inih/t.c
      ${p.inih}/bin/portix-run sh -c 'cd /tmp/inih && gcc t.c $(pkg-config --cflags --libs inih) -o t && ./t'
    '';
    # The image is the package, not everything its merge needed: go is a BDEPEND.
    image-scope = test "image-scope" [ ] ''
      test -f ${p.cronlocker}/image/usr/bin/cronlocker
      if ls ${p.cronlocker}/image/var/db/pkg | grep -q dev-lang; then ${fail "cronlocker's image carries a toolchain"}; fi
    '';
    # A NEW SLOT of a package the root has (docbook-xml-dtd:4.3 beside 4.1.2, 4.2, 4.5):
    # under a boundary that cannot say "slot", the plan loses it and the merge dies
    # reaching for the network, hours in. Checked at evaluation.
    slot-dep = test "slot-dep" [ ] ''
      ${lib.optionalString (!has "app-text/docbook-xml-dtd-4\\.3.*" (plan p.bubblewrap)) (fail "bubblewrap's plan lost docbook-xml-dtd:4.3")}
      ${p.bubblewrap}/bin/bwrap --version | grep -q bubblewrap
    '';
    # The launcher must not take a user namespace away from its program (a chroot
    # would: the kernel refuses CLONE_NEWUSER to a chrooted process).
    userns = test "userns" [ ] ''
      ${p.bubblewrap}/bin/bwrap --unshare-user --bind / / /bin/true || { ${fail "no nested user namespace inside the launcher"}; }
    '';
    # A fold over the installed set is regenerated over the closure: mime.cache is in
    # nobody's CONTENTS, and so is the ld.so.cache the root's loader reads.
    aggregate = test "aggregate" [ ] ''
      ${p.shared-mime-info}/bin/portix-run sh -c 'test -s /usr/share/mime/mime.cache && test -s /etc/ld.so.cache'
    '';
    # The identity a store path cannot hold is RECORDED against the image path: the
    # ebuild's own fowners in src_install and upstream's chgrp/chmod g+s in its make
    # install alike, every row image-relative, nothing from the build tree.
    identity = test "identity" [ ] ''
      d=${p.mlocate}/identity/dropped
      cat "$d"
      grep -q $'\tvar/lib/mlocate$' "$d" || { ${fail "fowners on /var/lib/mlocate was not recorded"}; }
      grep -q $'^mode\t2755\tusr/bin/locate$' "$d" || { ${fail "locate's setgid was not recorded"}; }
      if cut -f3 "$d" | grep -qv '^[^/]'; then ${fail "a row names a path that is not image-relative"}; fi
    '';
    # A program outside /usr/bin runs through portix-run: a prebuilt ELF whose
    # PT_INTERP is /lib64/ld-linux-x86-64.so.2, which exists only in the root.
    opt-bin = test "opt-bin" [ ] ''
      ${p.upx-bin}/bin/portix-run /opt/bin/upx --version | grep -q '^upx '
    '';
    # An install under /etc/portage reaches the image, though /etc/portage is portix's
    # config bound over the root while a node merges.
    savedconfig = test "savedconfig" [ ] ''
      grep -q KEY_DETACH ${p.abduco}/image/etc/portage/savedconfig/app-misc/abduco-*
    '';
    # A portix font is usable by nix's own fontconfig.
    dejavu = test "dejavu" [ pkgs.fontconfig ] ''
      export FONTCONFIG_FILE=${pkgs.makeFontsConf { fontDirectories = [ "${p.dejavu}/image" ]; }}
      fc-match "DejaVu Sans" | grep -qi dejavu
    '';
    # `select` binds a `||` choice, and `mask` a version choice; neither can do the
    # other's job. Two plans each, differing in one line, nothing built.
    select =
      let d = plan p.sbcl-bootstrap; c = plan p.sbcl-bootstrap-clisp; in
      test "select" [ ] ''
        ${lib.optionalString (!has "dev-lisp/ecl-.*" d) (fail "the default bootstrap is no longer ecl")}
        ${lib.optionalString (has "dev-lisp/clisp-.*" d) (fail "clisp is in the default plan")}
        ${lib.optionalString (!has "dev-lisp/clisp-.*" c) (fail "select did not put clisp in the closure")}
        ${lib.optionalString (has "dev-lisp/ecl-.*" c) (fail "select did not displace ecl")}
      '';
    mask =
      let m = plan p.acl-masked; in
      test "mask" [ ] ''
        ${lib.optionalString (lib.elem aclCpv m) (fail "the mask did not bind")}
        ${lib.optionalString (!has "sys-apps/acl-.*" m) (fail "the mask left no acl in the plan")}
      '';
    # Every ELF in every runtime layer resolves inside the root its launcher supplies.
    # Each subject must contribute objects: a check that measures nothing while
    # reporting success is how four examples once stayed green testing nothing.
    elf-resolution = test "elf-resolution" [ ] (lib.concatStrings (lib.mapAttrsToList (name: pkg: ''
      : > /tmp/elf-${name}
      for layer in ${toString (lib.init pkg.stack)}; do
        find -L "$layer" -type f \( -perm -u+x -o -name '*.so*' \) -print0 | while IFS= read -r -d "" f; do
          if [ "$(head -c4 "$f" | od -An -tx1 | tr -d ' ')" = 7f454c46 ]; then printf '%s\n' "''${f#"$layer"}" >> /tmp/elf-${name}; fi
        done
      done
      [ -s /tmp/elf-${name} ] || { ${fail "${name}: no ELF object found, so nothing was checked"}; }
      echo "${name}: $(wc -l < /tmp/elf-${name}) ELF objects"
      ${pkg}/bin/portix-run sh -c 'rc=0; while read -r f; do out=$(ldd "$f" 2>&1) || continue; case "$out" in *"not found"*) echo "UNRESOLVED $f"; echo "$out" | grep "not found"; rc=1;; esac; done < /tmp/elf-${name}; exit $rc'
    '') { inherit (p) figlet wget acl cffi inih mlocate bubblewrap shared-mime-info abduco; }));
  };
in
{ inherit packages tests; }
