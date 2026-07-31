# home-manager module for sagent.
#
# It lives here rather than in the consuming config so that a change to the
# CLI and the change to how the service invokes it land in one commit. When
# this lived downstream, every interface change needed two coordinated PRs,
# and a copy of it in the README drifted until it documented a silent bug.
#
# Import it as `inputs.sagent.homeModules.default` and set policy only:
#
#   imports = [ inputs.sagent.homeModules.default ];
#   services.sagent = { enable = true; maxPerHour = 7; };
{ self }:
{ lib, config, pkgs, ... }@args:

let
  cfg = config.services.sagent;

  # `osConfig` exists only when home-manager runs as a NixOS module. Guard it
  # so the module also evaluates in a standalone home-manager config.
  hostFromOs =
    if args ? osConfig then (args.osConfig.networking.hostName or null) else null;

  defaultPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.default;

  launcher = pkgs.writeShellScript "sagent-launcher" ''
    set -eu
    ${lib.optionalString (cfg.apiKeyFile != null) ''
      if [ -s "${toString cfg.apiKeyFile}" ]; then
        ANTHROPIC_API_KEY="$(${pkgs.coreutils}/bin/cat "${toString cfg.apiKeyFile}")"
        export ANTHROPIC_API_KEY
      fi
    ''}
    exec ${cfg.package}/bin/sagent watch-all \
      --model ${lib.escapeShellArg cfg.model} \
      --max-per-hour ${toString cfg.maxPerHour} \
      --rate-limit-cooldown ${toString cfg.rateLimitCooldown} \
      ${lib.escapeShellArgs cfg.extraArgs}
  '';
in
{
  options.services.sagent = {
    enable = lib.mkEnableOption "sagent — coding-agent session scribe";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "inputs.sagent.packages.\${system}.default";
      description = "The sagent package to run.";
    };

    hostname = lib.mkOption {
      type = lib.types.str;
      default = if hostFromOs != null then hostFromOs else "unknown-host";
      defaultText = lib.literalExpression "osConfig.networking.hostName";
      description = ''
        Name this machine writes under. Digests are keyed `<host>/<project>`,
        so a synced vault holding several machines needs this to differ.
      '';
    };

    outDir = lib.mkOption {
      type = lib.types.str;
      default = "${config.home.homeDirectory}/Obsidian/sagent/${cfg.hostname}";
      defaultText = lib.literalExpression ''"''${config.home.homeDirectory}/Obsidian/sagent/''${hostname}"'';
      description = "Root directory for digest output.";
    };

    model = lib.mkOption {
      type = lib.types.str;
      default = "claude-haiku-4-5";
      description = "Model id used for digest generation.";
    };

    apiKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/var/lib/opnix/secrets/anthropicApiKey";
      description = ''
        Optional. Path to a file whose contents are the raw Anthropic API key
        (no `KEY=` prefix). When set, the launcher exports ANTHROPIC_API_KEY
        and sagent bills that key per token. When null, the Agent SDK uses the
        user's Claude Code subscription auth and no key is needed.
      '';
    };

    maxPerHour = lib.mkOption {
      type = lib.types.int;
      default = 0;
      example = 7;
      description = ''
        Cap on LLM calls per rolling hour. 0 disables the cap. Every
        per-session digest and every project roll-up counts as one call.

        Hosts sharing one subscription share the quota, so budget the sum
        across machines rather than per machine. `watch-all` sweeps opencode
        as well as Claude Code, so there is more backlog at the same rate --
        the cap still holds, it just takes longer to catch up.
      '';
    };

    rateLimitCooldown = lib.mkOption {
      type = lib.types.int;
      default = 1800;
      description = ''
        Seconds to sleep after a rate-limit error before resuming. A throttled
        session is not marked done, so it is retried on the next pass.
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "--harness" "claude-code" "--db-poll-seconds" "60" ];
      description = "Additional arguments appended to `sagent watch-all`.";
    };

    extraPath = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
      description = ''
        Extra packages to place on the service PATH, on top of the runtime
        dependencies the module already provides.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    systemd.user.services.sagent = {
      Unit = {
        Description = "sagent — coding-agent session scribe (Claude Code + opencode)";
        After = [ "default.target" ];
      };

      Service = {
        Type = "simple";
        ExecStart = "${launcher}";
        Environment = [
          "SAGENT_OUT=${cfg.outDir}"
          # git is a RUNTIME dependency, not only a build one: rebrand
          # detection shells out to `git remote get-url origin`, and
          # git_remote_url swallows the resulting OSError and returns None
          # when git is missing. That fails silently -- it is indistinguishable
          # from "this directory has no origin remote", so the symptom is
          # `remote_url: null` on every digest forever and no log line.
          #
          # opencode is deliberately absent: sagent resolves it as
          # $OPENCODE_BIN, then ~/.opencode/bin/opencode, before consulting
          # PATH. The installer-managed binary is the one that writes the
          # database, and a differently-versioned build could read it at a
          # schema it did not write.
          "PATH=${config.home.homeDirectory}/.local/bin:${
            lib.makeBinPath ([ pkgs.coreutils pkgs.git ] ++ cfg.extraPath)
          }"
          "HOME=${config.home.homeDirectory}"
        ];
        Restart = "on-failure";
        RestartSec = "30s";
      };

      Install.WantedBy = [ "default.target" ];
    };
  };
}
