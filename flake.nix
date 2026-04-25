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

        claude-agent-sdk = python.pkgs.buildPythonPackage rec {
          pname = "claude-agent-sdk";
          version = "0.1.66";
          pyproject = true;
          src = python.pkgs.fetchPypi {
            pname = "claude_agent_sdk";
            inherit version;
            hash = "sha256-uPsUGYRW9Tb3FzOk2oTsecRNf5Gwb/NKR4YIehsEPEg=";
          };
          nativeBuildInputs = with python.pkgs; [ hatchling ];
          propagatedBuildInputs = with python.pkgs; [ anyio mcp ];
          pythonImportsCheck = [ "claude_agent_sdk" ];
          doCheck = false;
        };

        sagent = python.pkgs.buildPythonApplication {
          pname = "sagent";
          version = "0.8.0";
          src = ./.;
          pyproject = true;
          nativeBuildInputs = with python.pkgs; [ hatchling ];
          propagatedBuildInputs = [ claude-agent-sdk ];
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
