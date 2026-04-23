{
  description = "Custom Alpine APK repository (SolAstrius)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let pkgs = nixpkgs.legacyPackages.${system};
      in {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            zig             # cross-compiler for every Zig-based package
            abuild          # Alpine package builder
            apk-tools       # apk index / verify
            openssl         # signing-key generation
            gnumake
            jq
            git
            gh
          ];
          shellHook = ''
            export ABUILD_USERDIR="''${ABUILD_USERDIR:-$PWD/.abuild}"
            echo "alpine-apks devshell — zig $(zig version 2>/dev/null || echo '?'), abuild $(command -v abuild >/dev/null && echo ok || echo missing)"
          '';
        };
      });
}
