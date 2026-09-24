{
  description = "Vice - instant-replay game clipping for Linux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      overlays.default = final: prev: {
        vice-clipper = final.callPackage ./nix/package.nix { inherit (final.xorg) xprop; };
      };

      packages = forAllSystems (pkgs: rec {
        vice-clipper = pkgs.callPackage ./nix/package.nix { inherit (pkgs.xorg) xprop; };
        default = vice-clipper;
      });

      nixosModules.default = { ... }: {
        imports = [ ./nix/module.nix ];
        nixpkgs.overlays = [ self.overlays.default ];
      };

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          inputsFrom = [ self.packages.${pkgs.system}.vice-clipper ];
          packages = [ pkgs.nodejs pkgs.python3Packages.pytest ];
        };
      });
    };
}
