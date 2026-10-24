# portix's engine. Gentoo's signed stage3 is the binary seed of a bootstrap that
# discards it; the root it produces holds no store path; every package is then merged
# into a copy of that root by real, unmodified Portage and lifted out by its CONTENTS.
# SPEC.md is the argument. Comments here say what a piece is for, and they stay
# OUTSIDE the '' strings: a byte inside one is part of a derivation, and every rung
# and node rebuilds when it changes.
{ pkgs
, gentoo # the verified ebuild tree
, seed # the verified stage3 tarball
, portage # Portage's source, run as it is
, config ? { }
}:

let
  inherit (pkgs) lib;
  python = "${pkgs.python3}/bin/python3";
  tools = [ pkgs.python3 pkgs.gnutar pkgs.xz ];

  # One module run with exactly the sources it imports, so editing a module rebuilds
  # only what runs it.
  py = interpreter: module: imports:
    let src = lib.fileset.toSource {
      root = ./pysrc;
      fileset = lib.fileset.unions
        (map (m: ./pysrc/portix + "/${m}.py") ([ "__init__" module ] ++ imports));
    }; in
    "${interpreter} -c 'import sys; sys.path.insert(0, sys.argv[1]); from portix.${module} import main; sys.exit(main(sys.argv[2:]))' ${src}";

  # --- policy -----------------------------------------------------------------------
  # make.conf and Gentoo's per-package files, under Gentoo's own names. The ladder is
  # built from these defaults alone; a caller's `config` reaches packages and never the
  # root, so every policy shares one root.
  defaults = {
    makeConf = {
      ACCEPT_KEYWORDS = "amd64";
      CFLAGS = "-O2 -pipe";
      CXXFLAGS = "-O2 -pipe";
      FCFLAGS = "-O2 -pipe";
      FFLAGS = "-O2 -pipe";
    };
    packageUse = { }; # atom -> { flag = bool; }
    packageAcceptKeywords = { }; # atom -> "keywords"
    # GNU ar stamps the wall clock into each archive member, so no .a converges without
    # this. binutils' ebuild carries the flag commented out under "Things to think
    # about" -- an unmade decision, so portix makes it.
    packageEnv."sys-devel/binutils".EXTRA_ECONF = "--enable-deterministic-archives";
  };
  unknown = lib.subtractLists (lib.attrNames defaults) (lib.attrNames config);
  cfg = if unknown != [ ]
    then throw "portix: unknown config ${toString unknown}; the fields are ${toString (lib.attrNames defaults)}"
    else lib.mapAttrs (k: v: v // config.${k} or { }) defaults;

  # Mechanism, not policy, so a caller's FEATURES appends: nix already sandboxes and
  # Portage's own sandboxes need privileges a build lacks; userpriv and userfetch drop
  # to a second uid that does not exist; parallel-fetch reaches for a network there is
  # none of; preserve-libs keeps files no CONTENTS claims.
  features = [
    "-sandbox" "-usersandbox" "-network-sandbox" "-pid-sandbox" "-ipc-sandbox"
    "-userpriv" "-userfetch" "-ebuild-locks" "-distlocks" "-parallel-fetch" "-news"
    "-merge-sync" "-strict" "-preserve-libs"
  ];

  # The config root, bound over the root's own /etc/portage (see enterroot.py).
  mkConfig = name: c:
    let
      lines = f: m: lib.concatStringsSep "\n" (lib.mapAttrsToList f m);
      line = k: v: ''${k}="${v}"'';
      flags = fs: lib.concatStringsSep " " (lib.mapAttrsToList (f: on: lib.optionalString (!on) "-" + f) fs);
      file = lib.replaceStrings [ "/" ] [ "_" ];
    in
    pkgs.runCommand "portix-config-${name}" { } ''
      mkdir -p $out/repos.conf $out/env
      ln -s /var/db/repos/gentoo/profiles/default/linux/amd64/23.0 $out/make.profile
      cp ${portage}/cnf/sets/portage.conf $out/sets.conf
      cat > $out/repos.conf/gentoo.conf <<'EOF'
      [DEFAULT]
      main-repo = gentoo
      [gentoo]
      location = /var/db/repos/gentoo
      EOF
      cat > $out/make.conf <<'EOF'
      ${lines line (removeAttrs c.makeConf [ "FEATURES" ])}
      ${line "FEATURES" (toString ([ (c.makeConf.FEATURES or "") ] ++ features))}
      EOF
      cat > $out/package.use <<'EOF'
      ${lines (atom: fs: "${atom} ${flags fs}") c.packageUse}
      EOF
      cat > $out/package.accept_keywords <<'EOF'
      ${lines (atom: kw: "${atom} ${kw}") c.packageAcceptKeywords}
      EOF
      cat > $out/package.mask <<'EOF'
      ${lib.concatStringsSep "\n" (c.packageMask or [ ])}
      EOF
      ${lines (atom: vars: ''
        cat > $out/env/${file atom}.conf <<'EOF'
        ${lines line vars}
        EOF
        echo '${atom} ${file atom}.conf' >> $out/package.env
      '') c.packageEnv}
    '';
  rootConfig = mkConfig "root" defaults;
  # A node merges exactly the USE its plan resolved (written per node, below), so the
  # USE policy reaches nodes only through the plan, and a USE edit for one package does
  # not rebuild every other.
  nodeConfig = mkConfig "node" (cfg // { packageUse = { }; makeConf = removeAttrs cfg.makeConf [ "USE" ]; });
  # What a package resolves under: the instance policy, plus the target's own mask and
  # packageUse, which reach its plan and nothing else.
  planConfig = { mask, packageUse }: if mask == [ ] && packageUse == { } then mkConfig "plan" cfg
    else mkConfig "plan-target" (cfg // { packageUse = lib.recursiveUpdate cfg.packageUse packageUse; packageMask = mask; });

  # --- entering a root --------------------------------------------------------------
  # A root is unpacked or copied, never mounted from the store: overlayfs takes a
  # merged directory's mode and owner from its read-only, root-owned lower, so no
  # writable layer can create an entry in one.
  layRoot = lower: conf: ''
    root=$NIX_BUILD_TOP/root
    mkdir -p "$root"
    ${if lower == null then ''
      tar -xf ${seed} -C "$root" --exclude=./dev/console --exclude=./dev/null
    '' else ''
      tar -C ${lower} -cf - . | tar -C "$root" -xf -
      chmod -R u+w "$root"
    ''}
    config=$NIX_BUILD_TOP/config
    cp -r ${conf} "$config"
    chmod -R u+w "$config"
  '';
  # Each distfile is fixed-output on its Manifest SHA512; copied, because Portage
  # writes lock files beside them.
  fetchDistfile = d: pkgs.fetchurl { inherit (d) name urls sha512; };
  stageDistfiles = ''
    distdir=$NIX_BUILD_TOP/distdir
    mkdir -p "$distdir"
    for f in $distfiles; do cp "$f" "$distdir/$(stripHash "$f")"; done
  '';
  inRoot = script: args: ''
    ${py python "enterroot" [ ]} "$root" ${gentoo} "$distdir" "$config" /nix "$NIX_BUILD_TOP" -- /bin/bash ${script} ${args}
  '';
  # Inside the root. Every variable naming a store path is PORTAGE_*, which Portage
  # keeps out of saved ebuild environments. MAKEOPTS is nix's per-derivation budget,
  # read when the script runs (make.conf expansion never sees the environment).
  # LC_ALL=C: Portage probes the locale and sets C.UTF-8 only once the probe can run,
  # which, part-way through a rung, reorders every sorted list after that point.
  portageEnv = ''
    export PORTAGE_PYM_PATH=${portage}/lib PORTAGE_BIN_PATH=${portage}/bin
    export PORTAGE_USERNAME=root PORTAGE_GRPNAME=root PATH=/usr/bin LC_ALL=C
    export MAKEOPTS=-j$NIX_BUILD_CORES
  '';
  # Every Portage process runs under nosetid.c, built by the root's own compiler.
  noSetid = ''
    /usr/bin/gcc -O2 -shared -fPIC -o /var/tmp/portix-nosetid.so ${./nosetid.c}
    export LD_PRELOAD=/var/tmp/portix-nosetid.so PORTIX_NOSETID_LOG=/var/tmp/portix-nosetid.log
  '';
  tally = ''
    if [ -e "$root/var/tmp/portix-nosetid.log" ]; then n=$(wc -l < "$root/var/tmp/portix-nosetid.log"); else n=0; fi
    echo "portix: $n owner or setuid/setgid requests dropped; a store path holds neither"
  '';
  # Portage's env-update: a dependency laid down by tar brings its /etc/env.d fragment
  # but not the fold of it (llvm's LDPATH, as a line of ld.so.conf).
  envUpdate = ''/usr/bin/python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import portage; portage.env_update()' "$PORTAGE_PYM_PATH"'';

  # --- the plan, as one import-from-derivation --------------------------------------
  # Portage resolves inside a root, the only place the installed database it resolves
  # against exists; `target` is the ROOT it plans for. Every plan resolves in the SEED
  # (`lower` null): resolved in a rung's predecessor, merely evaluating a rung would
  # build the one below it.
  json = name: v: pkgs.writeText "portix-${name}.json" (builtins.toJSON v);
  planFor = { name, atoms, target, emptytree ? false, provided ? [ ], select ? [ ], config ? rootConfig }:
    lib.importJSON (pkgs.runCommand "portix-plan-${name}" { nativeBuildInputs = tools; distfiles = [ ]; } ''
      ${layRoot null config}
      ${stageDistfiles}
      cat > "$NIX_BUILD_TOP/plan.sh" <<'EOF'
      set -eu
      ${portageEnv}
      ${noSetid}
      ${py "/usr/bin/python3" "plan" [ ]} ${lib.optionalString emptytree "--emptytree"} ${lib.optionalString (provided != [ ]) "--provided ${json "provided" provided}"} ${lib.optionalString (select != [ ]) "--select ${json "select-${name}" select}"} ${target} ${lib.escapeShellArgs atoms} > "$1"
      EOF
      ${inRoot ''"$NIX_BUILD_TOP/plan.sh"'' ''"$out"''}
    '');

  # --- the ladder -------------------------------------------------------------------
  # stage1 merges the profile's own packages.build into an EMPTY $out from a Portage
  # running in the seed: SYSROOT unset makes ESYSROOT "/", so it compiles against the
  # seed and installs into the new root -- catalyst's stage1. Nothing in $out can come
  # from the seed except through a merge; the merged-usr links are the stage builder's
  # to lay (baselayout makes only the libdir ones), copied from the seed's own layout.
  # --jobs=1 because Portage orders deep system merges by build completion otherwise.
  packagesBuild = lib.filter (l: l != "" && !lib.hasPrefix "#" l)
    (lib.splitString "\n" (builtins.readFile "${gentoo}/profiles/default/linux/packages.build"));
  stage1 =
    let plan = planFor { name = "stage1"; atoms = packagesBuild; target = "/tmp/stage-root"; }; in
    pkgs.runCommand "portix-stage-1" { nativeBuildInputs = tools; distfiles = map fetchDistfile plan.distfiles; passthru = { inherit plan; }; } ''
      ${layRoot null rootConfig}
      ${stageDistfiles}
      mkdir "$out" "$out/usr" "$out/usr/bin" "$out/usr/lib" "$out/usr/lib64"
      ln -s bin "$out/usr/sbin"
      ln -s usr/bin "$out/bin"
      ln -s usr/bin "$out/sbin"
      ln -s usr/lib "$out/lib"
      ln -s usr/lib64 "$out/lib64"
      cat > "$NIX_BUILD_TOP/stage.sh" <<'EOF'
      set -eu
      ${portageEnv}
      ${noSetid}
      out=$1; shift
      "$PORTAGE_BIN_PATH/emerge" --root="$out" --oneshot --jobs=1 "$@"
      EOF
      ${inRoot ''"$NIX_BUILD_TOP/stage.sh"'' ''"$out" ${lib.escapeShellArgs packagesBuild}''}
      ${tally}
      rm -rf "$out/var/cache/edb"
      mkdir -p "$out/var/cache/edb"
      ${py python "check" [ "contents" ]} "$out"
    '';

  # The in-place rungs: `emerge --emptytree @system` with / = the rung below. In place
  # because an ebuild's [[ -z ${ROOT} ]] branches (locale-gen, baselayout's shape) run
  # only there. All share one plan, resolved in the seed: --emptytree plans as if
  # nothing were installed, so it cannot depend on which root resolves it. What is
  # emitted drops the bind mountpoints, PORTAGE_TMPDIR and Portage's and ldconfig's
  # caches -- mtimedb is how a store path once reached a root.
  systemPlan = planFor { name = "system"; atoms = [ "@system" ]; target = "/"; emptytree = true; };
  mkRebuild = name: lower: pkgs.runCommand "portix-rebuild-${name}"
    { nativeBuildInputs = tools; distfiles = map fetchDistfile systemPlan.distfiles; passthru = { plan = systemPlan; inherit lower; }; } ''
      ${layRoot lower rootConfig}
      ${stageDistfiles}
      cat > "$NIX_BUILD_TOP/stage.sh" <<'EOF'
      set -eu
      ${portageEnv}
      ${noSetid}
      "$PORTAGE_BIN_PATH/emerge" --emptytree --oneshot --jobs=1 @system
      EOF
      ${inRoot ''"$NIX_BUILD_TOP/stage.sh"'' ""}
      ${tally}
      rmdir "$root/nix" "$root$NIX_BUILD_TOP"
      rm -rf "$root/var/tmp" "$root/var/cache/edb" "$root/var/cache/ldconfig"
      mkdir -p "$root/var/tmp" "$root/var/cache/edb" "$root/var/cache/ldconfig" "$out"
      tar -C "$root" -cf - . | tar -C "$out" -xf -
      ${py python "check" [ "contents" ]} "$out"
    '';
  # rebuild1 is transitional -- merged into a root still filling, its early packages
  # built by stage1's toolchain -- so rebuild2, the first rung built end to end
  # against a complete root, is the one shipped, and rebuild3 exists to be compared
  # with it. That is measured, not chosen: 97 content differences 1/2, none 2/3.
  rebuild1 = mkRebuild "1" stage1;
  rebuild2 = mkRebuild "2" rebuild1;
  rebuild3 = mkRebuild "3" rebuild2;
  root = rebuild2;
  # Convergence is its own cheap derivation so that the admissions in diff.py cost a
  # digest pass to change, never a rung. It proves the ladder stopped moving; it is not
  # the proof the seed was discarded, which is structural (stage1 starts empty).
  compare = name: a: b: gate: pkgs.runCommand "portix-convergence-${name}" { } ''
    ${py python "diff" [ ]} ${lib.optionalString (!gate) "--report-only"} ${a} ${b} | tee "$out"
  '';
  convergence12 = compare "1-2" rebuild1 rebuild2 false;
  convergence = compare "2-3" rebuild2 rebuild3 true;

  # --- licences ---------------------------------------------------------------------
  # LICENSE is a Gentoo expression, not an SPDX id, and is carried verbatim. `free` is
  # what nixpkgs' gate reads: free only if EVERY token is in Gentoo's @FREE group, so an
  # unknown or non-free one makes the consumer opt in.
  licenseGroups = lib.listToAttrs (map
    (l: let f = lib.filter (x: x != "") (lib.splitString " " l); in lib.nameValuePair (lib.head f) (lib.tail f))
    (lib.filter (l: l != "" && !lib.hasPrefix "#" l)
      (lib.splitString "\n" (builtins.readFile "${gentoo}/profiles/license_groups"))));
  expandGroup = g: lib.concatMap (m: if lib.hasPrefix "@" m then expandGroup (lib.removePrefix "@" m) else [ m ])
    (licenseGroups.${g} or [ ]);
  freeLicenses = expandGroup "FREE";
  meta = q: {
    inherit (q) description homepage;
    license = {
      shortName = q.license;
      free = lib.all (t: lib.elem t freeLicenses) (lib.filter (t: t != "" && !lib.elem t [ "||" "(" ")" ] && !lib.hasSuffix "?" t)
        (lib.splitString " " (lib.replaceStrings [ "\n" "\t" ] [ " " " " ] q.license)));
    };
    platforms = [ "x86_64-linux" ];
  };

  # --- one derivation per package ---------------------------------------------------
  # A plan as a graph. `reach field cpv` is cpv and everything reachable along `field`,
  # in Portage's merge order. Build edges are followed only backwards in that order:
  # Gentoo's graphs have cycles, Portage breaks each by choosing an order, and one
  # derivation per package must follow it or evaluate forever.
  graph = plan: rec {
    byCpv = lib.listToAttrs (map (q: lib.nameValuePair q.cpv q) plan.packages);
    order = lib.listToAttrs (lib.imap0 (i: q: lib.nameValuePair q.cpv i) plan.packages);
    reach = field: cpv: lib.sort (a: b: order.${a} < order.${b}) (map (x: x.key) (builtins.genericClosure {
      startSet = [ { key = cpv; } ];
      operator = x: map (key: { inherit key; }) (lib.filter
        (d: byCpv ? ${d} && (field == "rdeps" || order.${d} < order.${x.key})) byCpv.${x.key}.${field});
    }));
  };

  # A node lays the root, copies in the trees of everything it builds against and
  # replays the identity lines they added, merges itself with --nodeps (resolution
  # happened once, in the plan) under exactly the plan's USE, and emits itself. It
  # names only what it is built from, so the same package in two closures is one
  # derivation. It is stricter than one merge into one root: under-declaring against a
  # sibling fails here, at compile.
  nodesFor = plan:
    let
      g = graph plan;
      dist = lib.listToAttrs (map (d: lib.nameValuePair d.name d) plan.distfiles);
      nodes = lib.mapAttrs
        (cpv: q: pkgs.stdenv.mkDerivation {
          inherit (q) pname version;
          nativeBuildInputs = tools;
          distfiles = map (n: fetchDistfile dist.${n}) q.distfiles;
          layers = map (d: nodes.${d}) (lib.remove cpv (g.reach "deps" cpv));
          dontUnpack = true;
          meta = meta q;
          buildCommand = ''
            ${layRoot root nodeConfig}
            ${lib.optionalString (q.useflags != "") ''echo ${lib.escapeShellArg "=${cpv} ${q.useflags}"} >> "$config/package.use"''}
            for d in $layers; do
              tar -C "$d/image" -cf - . | tar -C "$root" -xf - --delay-directory-restore
              (cd "$d/image" && find . -type d -print0) | (cd "$root" && xargs -0r chmod u+w)
              for f in passwd group shadow gshadow; do cat "$d/identity/$f" >> "$root/etc/$f"; done
            done
            mkdir "$NIX_BUILD_TOP/identity"
            cp "$root/etc/passwd" "$root/etc/group" "$root/etc/shadow" "$root/etc/gshadow" "$NIX_BUILD_TOP/identity/"
            ${stageDistfiles}
            cat > "$NIX_BUILD_TOP/build.sh" <<'EOF'
            set -eu
            ${portageEnv}
            ${noSetid}
            ${envUpdate}
            "$PORTAGE_BIN_PATH/emerge" --oneshot --nodeps --jobs=1 "=$2"
            unset LD_PRELOAD
            ${py python "emit" [ "contents" ]} / "$1" "$2" "$PORTIX_NOSETID_LOG" "$3"
            EOF
            ${inRoot ''"$NIX_BUILD_TOP/build.sh"'' ''"$out" ${lib.escapeShellArg cpv} "$NIX_BUILD_TOP/identity"''}
          '';
        })
        g.byCpv;
    in
    { inherit nodes; runtime = cpv: map (c: "${nodes.${c}}/image") (g.reach "rdeps" cpv); };

  # The fold over a closure's installed set -- mime.cache, gschemas.compiled,
  # ld.so.cache -- regenerated once per closure by re-running Portage over it (see
  # aggregate.py), and stacked last by every launcher.
  aggregateFor = name: layers: pkgs.stdenv.mkDerivation {
    name = "portix-aggregate-${name}";
    nativeBuildInputs = tools;
    distfiles = [ ];
    inherit layers;
    dontUnpack = true;
    buildCommand = ''
      ${layRoot root nodeConfig}
      for d in $layers; do
        tar -C "$d" -cf - . | tar -C "$root" -xf - --delay-directory-restore
        (cd "$d" && find . -type d -print0) | (cd "$root" && xargs -0r chmod u+w)
      done
      ${stageDistfiles}
      cat > "$NIX_BUILD_TOP/aggregate.sh" <<'EOF'
      set -eu
      ${portageEnv}
      ${noSetid}
      mkdir "$1"
      ${py python "aggregate" [ "check" "contents" ]} "$1" "$PORTAGE_BIN_PATH" "$PORTAGE_PYM_PATH" ${root} "$2"
      EOF
      ${inRoot ''"$NIX_BUILD_TOP/aggregate.sh"'' ''"$out" "$NIX_BUILD_TOP"''}
    '';
  };

  # --- what a caller uses -----------------------------------------------------------
  # The one command line every launcher runs: the root, a stack of layers over it,
  # host paths bound in. See launch.py.
  launchCommand = stack: bind: "${py python "launch" [ ]} ${lib.concatMapStrings (b: "--bind ${lib.escapeShellArg b} ") bind}${root} ${toString stack} --";

  # `emerge "cat/pkg"` or `emerge { atom; select; mask; packageUse; }`:
  #   select      atoms this closure CHOOSES where a `|| ( ... )` would otherwise take
  #               list order (sbcl's bootstrap Lisp)
  #   mask        what the resolver may not see -- the only answer to a version left
  #               ambient (`:*`, or `>=pkg-21` against a SLOT="0" package), and it must
  #               name the package the ambiguous atom REACHES
  #   packageUse  package.use for this target's plan alone
  # $out/bin holds the package's own programs plus portix-run for anything else (an
  # interpreter in the root, a program in /opt); $out/image is the package's own tree;
  # $out/identity is what it added to the account files and what the store dropped.
  emerge = spec: emergeTarget (if builtins.isString spec then { atom = spec; } else spec);
  emergeTarget = { atom, select ? [ ], mask ? [ ], packageUse ? { } }:
    let
      name = lib.strings.sanitizeDerivationName atom;
      plan = planFor {
        inherit name select;
        atoms = [ atom ];
        target = "/tmp/probe";
        provided = map (p: { inherit (p) cpv use; }) systemPlan.packages;
        config = planConfig { inherit mask packageUse; };
      };
      resolved = plan.requested.${atom};
      cpv = if lib.length resolved == 1 then lib.head resolved
        else throw "portix: ${atom} resolved to ${if resolved == [ ] then "nothing" else toString resolved}; name one package";
      built = nodesFor plan;
      layers = built.runtime cpv;
      stack = layers ++ [ "${aggregateFor name layers}" ];
      q = (graph plan).byCpv.${cpv};
    in
    pkgs.stdenv.mkDerivation {
      inherit (q) pname version;
      passthru = { inherit plan stack; };
      meta = meta q;
      dontUnpack = true;
      buildCommand = ''
        mkdir -p "$out/bin"
        ln -s ${built.nodes.${cpv}}/image "$out/image"
        ln -s ${built.nodes.${cpv}}/identity "$out/identity"
        printf '#!%s\nexec %s "$@"\n' ${pkgs.runtimeShell} ${lib.escapeShellArg (launchCommand stack [ ])} > "$out/bin/portix-run"
        while IFS= read -r -d "" exe; do
          printf '#!%s\nexec %q %q "$@"\n' ${pkgs.runtimeShell} "$out/bin/portix-run" "/$exe" > "$out/bin/''${exe##*/}"
        done < ${built.nodes.${cpv}}/launchers
        chmod +x "$out"/bin/*
      '';
    };

  # A launcher for a program integrated with the host: `bind` names host paths the
  # program needs that the minimum does not carry (chromium's /etc/chromium/policies).
  launcher = { package, program, bind ? [ ], name ? baseNameOf program }:
    pkgs.writeShellScriptBin name ''exec ${launchCommand package.stack bind} ${lib.escapeShellArg program} "$@"'';
in
{
  inherit emerge launcher root;
  inherit stage1 rebuild1 rebuild2 rebuild3 convergence convergence12;
}
