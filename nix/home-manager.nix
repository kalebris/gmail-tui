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
  exe = lib.getExe cfg.package;
  # Real PATH wrappers beat shell aliases: aliases only appear after HM
  # regenerates bashrc/zshrc and a new interactive shell starts.
  profilePackages = map (
    name:
    pkgs.writeShellScriptBin "gmail-tui-${name}" ''
      exec ${exe} --profile ${lib.escapeShellArg name} "$@"
    ''
  ) cfg.profiles;
  loginPackages = lib.optionals (cfg.clientJson != null) (
    [
      (pkgs.writeShellScriptBin "gmail-tui-login" ''
        exec ${exe} login --client-json ${lib.escapeShellArg cfg.clientJson} "$@"
      '')
    ]
    ++ map (
      name:
      pkgs.writeShellScriptBin "gmail-tui-${name}-login" ''
        exec ${exe} --profile ${lib.escapeShellArg name} login --client-json ${lib.escapeShellArg cfg.clientJson} "$@"
      ''
    ) cfg.profiles
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

    storeDir = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = lib.literalExpression ''"''${config.home.homeDirectory}/mail/gmail-tui"'';
      description = ''
        Directory for the local mail store (SQLite index, body cache, OAuth
        tokens). When set, Home Manager writes
        <filename>~/.config/gmail-tui/config</filename> with a
        <literal>data</literal> line (the gmail-tui setting name) so
        <command>gmail-tui</command> and <option>profiles</option> wrappers use
        this path without <command>--data</command>. <literal>~</literal> is
        expanded by gmail-tui.
      '';
    };

    extraConfig = lib.mkOption {
      type = lib.types.lines;
      default = "";
      example = ''
        account ada@example.com
        account bea@example.com
        download ~/Downloads
      '';
      description = ''
        Extra lines appended to the managed
        <filename>~/.config/gmail-tui/config</filename> when
        <option>programs.gmail-tui.storeDir</option> is set (account order,
        download path, date formats, …).
      '';
    };

    clientJson = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = lib.literalExpression "config.age.secrets.gmail-tui-client.path";
      description = ''
        Path to a Google Desktop OAuth client JSON (often an agenix-decrypted
        secret). When set, installs <command>gmail-tui-login</command> and, for
        each <option>profiles</option> entry,
        <command>gmail-tui-&lt;name&gt;-login</command> on <envar>PATH</envar>,
        which run <command>login --client-json</command> against that file.
        Use a string path (not a Nix path) so the secret is not copied into the
        store.
      '';
    };

    profiles = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "work"
        "home"
      ];
      description = ''
        Named mail stores under <option>storeDir</option> (or the default store).
        For each name, installs <command>gmail-tui-&lt;name&gt;</command> on
        <envar>PATH</envar> as a small wrapper around
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

    home.packages = [ cfg.package ] ++ profilePackages ++ loginPackages;

    xdg.configFile."gmail-tui/config" = lib.mkIf (cfg.storeDir != null) {
      text = ''
        # Managed by Home Manager (programs.gmail-tui.storeDir).
        data ${cfg.storeDir}
        ${cfg.extraConfig}
      '';
    };

    xdg.configFile."gmail-tui/keys" = lib.mkIf (cfg.keysFile != null || cfg.keys != { }) (
      if cfg.keysFile != null then
        { source = cfg.keysFile; }
      else
        { text = keysText; }
    );
  };
}
