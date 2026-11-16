{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.gmail-tui;
  renderKeys = import ./keys-file.nix { inherit lib; };
  keysPackage =
    if cfg.keysFile != null then
      cfg.keysFile
    else if cfg.keys != { } then
      pkgs.writeText "gmail-tui-keys" (renderKeys cfg.keys)
    else
      null;
  profileNameOk =
    name:
    name != ""
    && name != "."
    && name != ".."
    && !(lib.hasInfix "/" name)
    && !(lib.hasInfix "\\" name);
  profilePackages = map (
    name:
    pkgs.writeShellScriptBin "gmail-tui-${name}" ''
      exec ${lib.getExe cfg.package} --profile ${lib.escapeShellArg name} "$@"
    ''
  ) cfg.profiles;
in
{
  options.programs.gmail-tui = {
    enable = lib.mkEnableOption "gmail-tui (Gmail TUI client)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.gmail-tui or (throw "programs.gmail-tui.package: set this, or apply gmail-tui.overlays.default");
      defaultText = lib.literalExpression "pkgs.gmail-tui";
      description = "gmail-tui package to install.";
    };

    profiles = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "work"
        "home"
      ];
      description = ''
        Named mail stores under the configured data directory. For each name,
        installs <command>gmail-tui-&lt;name&gt;</command> on
        <envar>PATH</envar> as a wrapper around
        <command>gmail-tui --profile &lt;name&gt;</command>.
      '';
    };

    keys = lib.mkOption {
      type = lib.types.attrsOf (lib.types.listOf lib.types.str);
      default = { };
      example = lib.literalExpression ''
        {
          quit = [ "q" ];
          archive = [ "e" ];
        }
      '';
      description = ''
        Keyboard shortcut overrides. Installed as a Nix store file and selected
        via <envar>GMAIL_TUI_KEYS</envar> so the binding set is portable across
        machines that share this configuration.
      '';
    };

    keysFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional keys file path; overrides <option>programs.gmail-tui.keys</option>.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.keysFile == null || cfg.keys == { };
        message = "programs.gmail-tui: set keys or keysFile, not both";
      }
      {
        assertion = lib.all profileNameOk cfg.profiles;
        message = "programs.gmail-tui.profiles: each entry must be a single directory name (no slashes)";
      }
    ];

    environment.systemPackages = [ cfg.package ] ++ profilePackages;

    environment.variables = lib.mkIf (keysPackage != null) {
      GMAIL_TUI_KEYS = "${keysPackage}";
    };
  };
}
