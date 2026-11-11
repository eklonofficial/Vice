# NixOS virtual machine test for #283. Boots a throw-away NixOS machine in
# QEMU with Vice enabled and checks that xdg-open is reachable from Vice.
#
#   nix-build nix/vm-test.nix        # from the repository root
#
# Needs Nix on Linux with KVM (/dev/kvm). The first run downloads a lot.
{ pkgs ? import <nixpkgs> { } }:

pkgs.testers.runNixOSTest {
  name = "vice-xdg-open";

  nodes.machine = { pkgs, ... }: {
    imports = [ ./module.nix ];
    nixpkgs.overlays = [
      (final: prev: {
        vice-clipper = final.callPackage ./package.nix {
          xprop = final.xprop or final.xorg.xprop;
        };
      })
    ];
    services.vice.enable = true;
    # Plain NixOS has no xdg-open for Vice unless the package brings it.
    environment.systemPackages = [ ];
  };

  testScript = ''
    machine.wait_for_unit("multi-user.target")

    vice = machine.succeed("readlink -f $(command -v vice)").strip()
    print("Vice wrapper:", vice)

    # The wrapper must put an xdg-utils bin directory on PATH.
    machine.succeed(f"grep -q xdg-utils {vice}")

    # And the xdg-open it points to must exist and run.
    xdg = machine.succeed(
        f"grep -o '/nix/store/[^:\"]*xdg-utils[^:\"]*/bin' {vice} | head -n 1"
    ).strip()
    machine.succeed(f"test -x {xdg}/xdg-open")
    machine.succeed(f"{xdg}/xdg-open --version")
  '';
}
