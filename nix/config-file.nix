# Render ~/.config/gmail-tui/config from programs.gmail-tui.settings.
# Null options are left out, so gmail-tui keeps its own default for that line.
{ lib }:
settings:
let
  command = argv: lib.concatMapStringsSep " " lib.escapeShellArg argv;
  onDemand =
    if settings.onDemand == null then
      [ ]
    else if settings.onDemand == [ ] then
      [ "on-demand none" ]
    else
      map (label: "on-demand ${label}") settings.onDemand;
  lines = lib.filter (line: line != "") (
    [
      (lib.optionalString (settings.download != null) "download ${settings.download}")
      (lib.optionalString (settings.data != null) "data ${settings.data}")
    ]
    ++ map (account: "account ${account}") settings.accounts
    ++ [
      (lib.optionalString (settings.date != null) "date ${settings.date}")
      (lib.optionalString (settings.time != null) "time ${settings.time}")
      (lib.optionalString (settings.datetime != null) "datetime ${settings.datetime}")
      (lib.optionalString (settings.weekday != null) "weekday ${settings.weekday}")
      (lib.optionalString (settings.oauthTpm != null) "oauth-tpm ${if settings.oauthTpm then "yes" else "no"}")
      (lib.optionalString (settings.oauthTpmIdentity != null) "oauth-tpm-identity ${settings.oauthTpmIdentity}")
    ]
    ++ onDemand
    ++ lib.optional (settings.signature != null) "signature ${settings.signature}"
    ++ lib.optional (settings.open != null) "open ${command settings.open}"
  );
in
lib.concatStringsSep "\n" lines
