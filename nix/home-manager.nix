{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.gmail-tui;
  renderKeys = import ./keys-file.nix { inherit lib; };
  keysText = renderKeys cfg.keys;
  profileNameOk =
    name:
    name != ""
    && name != "."
    && name != ".."
    && !(lib.hasInfix "/" name)
    && !(lib.hasInfix "\\" name);
  profileAliases = lib.listToAttrs (
    map (name: {
      name = "gmail-tui-${name}";
      value = "${lib.getExe cfg.package} --profile ${lib.escapeShellArg name}";
    }) cfg.profiles
  );
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
        a shell alias <literal>gmail-tui-&lt;name&gt;</literal> runs
        <command>gmail-tui --profile &lt;name&gt;</command>. Config and keys stay
        shared; only the mail index and tokens are separated.
      '';
    };

    keys = lib.mkOption {
      type = lib.types.attrsOf (lib.types.listOf lib.types.str);
      default = { };
      example = lib.literalExpression ''
        {
          quit = [ "q" "Q" ];
          down = [ "j" "down" ];
          archive = [ "e" ];
          inbox = [ "g-i" ];
        }
      '';
      description = ''
        Keyboard shortcuts written to <filename>~/.config/gmail-tui/keys</filename>.
        Only list actions you want to change; everything else keeps the built-in
        defaults. See <filename>gmail_tui/keys.py</filename> for action names.
      '';
    };

    keysFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Optional path to a complete keys file. When set, takes precedence over
        <option>programs.gmail-tui.keys</option>.
      '';
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

    home.packages = [ cfg.package ];

    home.shellAliases = lib.mkIf (cfg.profiles != [ ]) profileAliases;

    xdg.configFile."gmail-tui/keys" = lib.mkIf (cfg.keysFile != null || cfg.keys != { }) (
      if cfg.keysFile != null then
        { source = cfg.keysFile; }
      else
        { text = keysText; }
    );
  };
}
