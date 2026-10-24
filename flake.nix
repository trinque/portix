{
  description = "portix: Gentoo's signed tree and stage3, built by unmodified Portage into Nix derivations";

  # What portix's own suite builds with. A consumer that lets this follow its own
  # nixpkgs rebuilds the root whenever that moves (README: "Using it").
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  # The ebuild tree, the seed and Portage are the user's to choose. Every one is
  # verified against a pinned key before anything reads it (`verified`, below), so the
  # source of the bytes is not a trust decision.
  #
  # The tree follows Gentoo's latest signed snapshot, and that is the expected use:
  # `nix flake update` takes the newest. A consumer pins a dated one by overriding both:
  #   inputs.portix.inputs.gentooSnapshot.url = "file+https://distfiles.gentoo.org/snapshots/portage-YYYYMMDD.tar.xz";
  #   inputs.portix.inputs.gentooSnapshotSig.url = "file+https://distfiles.gentoo.org/snapshots/portage-YYYYMMDD.tar.xz.gpgsig";
  # `file+` keeps the bytes as published, so verification cannot be skipped.
  inputs.gentooSnapshot = { url = "file+https://distfiles.gentoo.org/snapshots/portage-latest.tar.xz"; flake = false; };
  inputs.gentooSnapshotSig = { url = "file+https://distfiles.gentoo.org/snapshots/portage-latest.tar.xz.gpgsig"; flake = false; };
  # The seed: any recent amd64 openrc stage3, which the bootstrap discards. Gentoo names
  # no latest tarball, only a signed pointer to a dated one (autobuilds/
  # latest-stage3-amd64-openrc.txt), so this is a dated pin, overridden the same way.
  inputs.gentooStage3 = { url = "file+https://distfiles.gentoo.org/releases/amd64/autobuilds/20261004T164559Z/stage3-amd64-openrc-20261004T164559Z.tar.xz"; flake = false; };
  inputs.gentooStage3Sig = { url = "file+https://distfiles.gentoo.org/releases/amd64/autobuilds/20261004T164559Z/stage3-amd64-openrc-20261004T164559Z.tar.xz.asc"; flake = false; };
  # A rolling file upstream, locked like the rest: a key rotation fails verification.
  inputs.gentooKeys = { url = "file+https://qa-reports.gentoo.org/output/service-keys.gpg"; flake = false; };
  # Portage follows its default branch, with the tree.
  inputs.portage = { url = "github:gentoo/portage"; flake = false; };

  outputs = { self, nixpkgs, gentooSnapshot, gentooSnapshotSig, gentooStage3, gentooStage3Sig, gentooKeys, portage }:
    let
      inherit (nixpkgs) lib;
      system = "x86_64-linux";

      # A tree exists only BY verifying it: everything downstream reads this output,
      # never the input. VALIDSIG's first and last fields pin the signing subkey AND its
      # primary, so a good signature from any other key in Gentoo's keyring fails, and
      # GOODSIG excludes an expired or revoked one.
      verified = pkgs: { name, file, sig, key, primary, install }:
        pkgs.runCommand name { nativeBuildInputs = [ pkgs.gnupg pkgs.gnutar pkgs.xz ]; } ''
          export GNUPGHOME=$TMPDIR/gnupg
          mkdir -m 700 "$GNUPGHOME"
          gpg --batch --quiet --import ${gentooKeys}
          if ! gpg --batch --status-fd 3 --verify ${sig} ${file} 3>"$TMPDIR/status" >"$TMPDIR/log" 2>&1; then
            cat "$TMPDIR/log" >&2
            exit 1
          fi
          if ! grep -q '^\[GNUPG:\] GOODSIG ' "$TMPDIR/status" || ! grep -q "^\[GNUPG:\] VALIDSIG ${key} .* ${primary}$" "$TMPDIR/status"; then
            echo "portix: ${name} is not signed by ${key} (primary ${primary})." >&2
            echo "portix: if Gentoo rotated keys, verify the new one out of band before pinning it." >&2
            grep '^\[GNUPG:\]' "$TMPDIR/status" >&2
            exit 1
          fi
          ${install}
        '';
      # The ebuild tree, with the md5-cache the snapshot ships (the git tree omits it,
      # and every metadata read would otherwise run an ebuild).
      gentooFor = pkgs: verified pkgs {
        name = "gentoo-portage-snapshot";
        file = gentooSnapshot;
        sig = gentooSnapshotSig;
        key = "E1D6ABB63BFCFB4BA02FDF1CEC590EEAC9189250"; # Gentoo Portage Snapshot Signing Key
        primary = "DCD05B71EAB94199527F44ACDB6B8C1F96D8BF6D";
        install = ''
          mkdir "$out"
          tar -xJf ${gentooSnapshot} -C "$out" --strip-components=1
          if [ ! -d "$out/metadata/md5-cache" ]; then echo "portix: the snapshot has no metadata/md5-cache" >&2; exit 1; fi
        '';
      };
      # The binary seed the bootstrap discards. A tarball, linked rather than copied:
      # every rung unpacks its own writable root from it in seconds.
      seedFor = pkgs: verified pkgs {
        name = "portix-stage3-seed";
        file = gentooStage3;
        sig = gentooStage3Sig;
        key = "534E4209AB49EEE1C19D96162C44695DB9F6043D"; # Gentoo Linux Release Engineering (Automated Weekly Release Key)
        primary = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910";
        install = ''ln -s ${gentooStage3} "$out"'';
      };

      # portix under a caller's make.conf policy. The tools that lay the root come from
      # portix's own nixpkgs, so the root is one per nixpkgs and not one per consumer;
      # of the caller's `pkgs` only `config` is read, because a licence policy is theirs.
      mkPortix = callerPkgs: config:
        let pkgs = import nixpkgs { inherit system; config = callerPkgs.config or { }; }; in
        assert lib.assertMsg (callerPkgs.stdenv.hostPlatform.system == system)
          "portix builds for ${system} only: its seed is Gentoo's amd64 stage3";
        import ./stage.nix { inherit pkgs config portage; gentoo = gentooFor pkgs; seed = seedFor pkgs; };

      pkgs = nixpkgs.legacyPackages.${system};
      suite = import ./suite.nix { inherit pkgs; portix = mkPortix pkgs; };
    in
    {
      lib = { inherit mkPortix; };

      packages.${system} = suite.packages // {
        inherit (mkPortix pkgs { }) stage1 rebuild1 rebuild2 rebuild3 convergence convergence12;
      };

      # What compiles nothing. `nix flake check` also evaluates every hydraJobs
      # derivation, which resolves every suite plan -- the evaluation gate.
      checks.${system}.python = pkgs.runCommand "portix-python"
        { nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.mypy ps.pytest ])) ]; } ''
        cp -r ${./pysrc} src
        chmod -R u+w src
        cd src
        mypy --config-file pyproject.toml
        python -m pytest -q -p no:cacheprovider tests
        touch $out
      '';

      # Every example built AND asserted, and the ladder's convergence. Hours.
      hydraJobs = suite.tests // { inherit (self.packages.${system}) convergence; };
    };
}
