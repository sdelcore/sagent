{
  description = "sagent — watches Claude Code sessions and writes understanding digests";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;

        sagent = python.pkgs.buildPythonApplication {
          pname = "sagent";
          version = "0.2.0";
          src = ./.;
          pyproject = true;
          nativeBuildInputs = with python.pkgs; [ hatchling ];
          propagatedBuildInputs = with python.pkgs; [ anthropic ];
          doCheck = true;
          nativeCheckInputs = [ python.pkgs.pytestCheckHook ];
          pythonImportsCheck = [
            "sagent"
            "sagent.cli"
            "sagent.parser"
            "sagent.understand"
          ];
        };
      in {
        packages.default = sagent;
        packages.sagent = sagent;

        apps.default = {
          type = "app";
          program = "${sagent}/bin/sagent";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
          ];

          shellHook = ''
            export UV_PYTHON=${python}/bin/python3
            export UV_PYTHON_DOWNLOADS=never
          '';
        };
      });
}
