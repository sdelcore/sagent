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
          version = "0.1.0";
          src = ./.;
          pyproject = true;
          nativeBuildInputs = with python.pkgs; [ hatchling ];
          # No runtime python deps — the LLM backend shells out to `claude`.
          # Users who want the SDK path can install the `sdk` extra.
          propagatedBuildInputs = [ ];
          # The `claude` CLI is not declared as a dep so sagent works with
          # --no-llm or user-provided PATH. Downstream nix modules that want
          # the subscription backend should add claude-code to the user PATH.
          doCheck = true;
          nativeCheckInputs = [ python.pkgs.pytestCheckHook ];
          pythonImportsCheck = [ "sagent" "sagent.cli" "sagent.parser" ];
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
