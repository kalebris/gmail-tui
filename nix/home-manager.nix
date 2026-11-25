{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.gmail-tui;
  renderKeys = import ./keys-file.nix { inherit lib; };
  renderConfig = import ./config-file.nix { inherit lib; };
  keysText = renderKeys cfg.keys;
  profileNameOk =
    name:
    name != ""
    && name != "."
    && name != ".."
    && !(lib.hasInfix "/" name)
    && !(lib.hasInfix "\\" name);
  command = argv: lib.concatMapStringsSep " " lib.escapeShellArg argv;
  exe = lib.getExe cfg.package;
  profileNames = lib.attrNames cfg.profiles;
  # Real PATH wrappers beat shell aliases: aliases only appear after HM
  # regenerates bashrc/zshrc and a new interactive shell starts.
  profilePackages = map (
    name:
    pkgs.writeShellScriptBin "gmail-tui-${name}" ''
      exec ${exe} --profile ${lib.escapeShellArg name} "$@"
    ''
  ) profileNames;
  # clientJson is often an agenix home path like
  # "${XDG_RUNTIME_DIR}/agenix/…". Double-quote so the shell expands it;
  # escapeShellArg would single-quote and leave the literal unevaluated.
  loginPackages = lib.optionals (cfg.clientJson != null) (
    [
      (pkgs.writeShellScriptBin "gmail-tui-login" ''
        exec ${exe} login --client-json "${cfg.clientJson}" "$@"
      '')
    ]
    ++ map (
      name:
      pkgs.writeShellScriptBin "gmail-tui-${name}-login" ''
        exec ${exe} --profile ${lib.escapeShellArg name} login --client-json "${cfg.clientJson}" "$@"
      ''
    ) profileNames
  );
  renderedSettings = renderConfig cfg.settings;
  extraLines = lib.filter (line: line != "") (lib.splitString "\n" cfg.extraConfig);
  configText =
    if renderedSettings == "" && extraLines == [ ] then
      ""
    else
      lib.concatStringsSep "\n" (
        [ "# Managed by Home Manager (programs.gmail-tui.settings)." ]
        ++ lib.optional (renderedSettings != "") renderedSettings
        ++ extraLines
      )
      + "\n";
  profileFiles = lib.mapAttrs' (
    name: profile:
    lib.nameValuePair "gmail-tui/profiles/${name}" {
      text = ''
        # Managed by Home Manager (programs.gmail-tui.profiles.${name}.open).
        # Links and HTML parts. Attachments and other files stay on xdg-open.
        open ${command profile.open}
      '';
    }
  ) (lib.filterAttrs (_: profile: profile.open != null) cfg.profiles);
  emptyOpens =
    lib.optional (cfg.settings.open == [ ]) "programs.gmail-tui.settings.open needs a command"
    ++ lib.concatLists (
      lib.mapAttrsToList (
        name: profile: lib.optional (profile.open == [ ]) "programs.gmail-tui.profiles.${name}.open needs a command"
      ) cfg.profiles
    );
  badProfiles = lib.filter (name: !profileNameOk name) profileNames;
  badAccounts = lib.filter (
    addr: addr == "" || builtins.match ".*[[:space:]].*" addr != null
  ) cfg.settings.accounts;
in
{
  imports = [
    (lib.mkRenamedOptionModule
      [ "programs" "gmail-tui" "storeDir" ]
      [ "programs" "gmail-tui" "settings" "data" ]
    )
  ];

  options.programs.gmail-tui = {
    enable = lib.mkEnableOption "gmail-tui (Gmail TUI client)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.gmail-tui or (throw "programs.gmail-tui.package: set this, or apply gmail-tui.overlays.default");
      defaultText = lib.literalExpression "pkgs.gmail-tui";
      description = "gmail-tui package to install.";
    };

    settings = lib.mkOption {
      type = lib.types.submodule {
        options = {
          data = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = lib.literalExpression ''"''${config.home.homeDirectory}/mail/gmail-tui"'';
            description = ''
              Mail store root (the <literal>data</literal> line). <literal>~</literal>
              is expanded by gmail-tui. Profiles are directories under this path.
              Formerly <option>programs.gmail-tui.storeDir</option>.
            '';
          };

          download = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "~/Downloads";
            description = "Directory for saved attachments. Null keeps gmail-tui's default.";
          };

          accounts = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ ];
            example = [
              "ada@example.com"
              "bea@example.com"
            ];
            description = ''
              Mailbox addresses, left to right on the top bar. Any mailbox omitted
              here still appears, after these.
            '';
          };

          date = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "%Y-%m-%d";
            description = "strftime for calendar days and all-day stamps.";
          };

          time = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "%H:%M";
            description = "strftime for hour labels.";
          };

          datetime = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "%Y-%m-%d %H:%M";
            description = "strftime for mail and event stamps. Null uses date plus time.";
          };

          weekday = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "%a %d";
            description = "strftime for week columns.";
          };

          oauthTpm = lib.mkOption {
            type = lib.types.nullOr lib.types.bool;
            default = null;
            description = ''
              Encrypt each account's <filename>oauth.json</filename> with a TPM age key.
              Null leaves the line out.
            '';
          };

          oauthTpmIdentity = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "~/.config/gmail-tui/oauth-tpm.identity";
            description = "Path to the age-plugin-tpm identity file.";
          };

          onDemand = lib.mkOption {
            type = lib.types.nullOr (lib.types.listOf lib.types.str);
            default = null;
            example = [
              "TRASH"
              "SPAM"
            ];
            description = ''
              Labels whose bodies wait until that folder is open. Null keeps the
              app default (TRASH and SPAM). An empty list fetches every folder.
            '';
          };

          signature = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "~/.config/gmail-tui/signature";
            description = ''
              Path to a signature file. Its text is inserted into a new message
              or a reply, above any quoted text. Null leaves the line out.
            '';
          };

          open = lib.mkOption {
            type = lib.types.nullOr (lib.types.listOf lib.types.str);
            default = null;
            example = [
              "firefox"
              "-P"
              "settings"
            ];
            description = ''
              Command that opens a link or an HTML part, shared by every profile.
              The URL or file is appended. Null means <command>xdg-open</command>.
              Attachments and other files stay on <command>xdg-open</command>.
              A profile's
              <option>programs.gmail-tui.profiles.&lt;name&gt;.open</option>
              overrides it for that profile.
            '';
          };
        };
      };
      default = { };
      description = ''
        Shared settings written to <filename>~/.config/gmail-tui/config</filename>.
        Options left unset are omitted, and gmail-tui keeps its built-in default.
        Setting any of these replaces that file on the next Home Manager switch.
      '';
    };

    extraConfig = lib.mkOption {
      type = lib.types.lines;
      default = "";
      example = ''
        # lines the structured settings do not cover yet
      '';
      description = ''
        Raw lines appended after <option>programs.gmail-tui.settings</option>
        in <filename>~/.config/gmail-tui/config</filename>.
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
      type = lib.types.coercedTo (lib.types.listOf lib.types.str) (
        names: lib.genAttrs names (_: { })
      ) (lib.types.attrsOf (lib.types.submodule ({ name, ... }: {
        options.open = lib.mkOption {
          type = lib.types.nullOr (lib.types.listOf lib.types.str);
          default = null;
          description = ''
            Command that opens a link or an HTML part for the <literal>${name}</literal> profile.
            The URL or file is appended. Written to
            <filename>~/.config/gmail-tui/profiles/${name}</filename>, which
            overrides <option>programs.gmail-tui.settings.open</option>.
            Attachments and other files stay on <command>xdg-open</command>.
          '';
          example = [
            "firefox"
            "-P"
            "settings"
          ];
        };
      })));
      default = { };
      example = lib.literalExpression ''
        {
          work.open = [ "firefox" "-P" "settings" ];
          home.open = [ "firefox" "-P" "personal" ];
        }
      '';
      description = ''
        Named mail stores under <option>programs.gmail-tui.settings.data</option>
        (or the default store). For each name, installs
        <command>gmail-tui-&lt;name&gt;</command> on <envar>PATH</envar> as a
        wrapper around <command>gmail-tui --profile &lt;name&gt;</command>.
        <literal>open</literal> is the link command for that profile.
        A list of names is still accepted and means no per-profile link command:

        <literal>[ "work" "home" ]</literal>
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
        assertion = badProfiles == [ ];
        message = "programs.gmail-tui.profiles: each name must be a single directory name (no slashes): ${lib.concatStringsSep ", " badProfiles}";
      }
      {
        assertion = emptyOpens == [ ];
        message = lib.concatStringsSep "\n" emptyOpens;
      }
      {
        assertion = badAccounts == [ ];
        message = "programs.gmail-tui.settings.accounts: each entry is one address: ${lib.concatStringsSep ", " badAccounts}";
      }
    ];

    home.packages = [ cfg.package ] ++ profilePackages ++ loginPackages;

    xdg.configFile = lib.mkMerge [
      (lib.mkIf (configText != "") {
        "gmail-tui/config".text = configText;
      })
      (lib.mkIf (cfg.keysFile != null || cfg.keys != { }) {
        "gmail-tui/keys" =
          if cfg.keysFile != null then
            { source = cfg.keysFile; }
          else
            { text = keysText; };
      })
      profileFiles
    ];
  };
}
