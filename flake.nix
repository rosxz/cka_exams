{
  description = "CKA Mock - LLM-generated CKA mock exams on Minikube";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      python = pkgs.python3.withPackages (ps: [
        ps.poetry-core
        ps.rich
        ps.openai
        ps.pyyaml
        ps.jsonschema
        ps.pytest
      ]);
      package = pkgs.python3Packages.callPackage ./package.nix { inherit self; };
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        buildInputs = [
          python
          package
          pkgs.kubernetes
          pkgs.minikube
          pkgs.kubernetes-helm
          pkgs.kustomize
          pkgs.openssl
          (pkgs.writeShellScriptBin "k" "exec ${pkgs.kubernetes}/bin/kubectl \"\$@\"")
        ];

        shellHook = ''
          echo "NIX Dev Environment - cka-mock"
          alias k='kubectl'
        '';
      };

      packages.${system} = {
        default = package;
      };
    };
}
