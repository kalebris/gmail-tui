# Render a gmail-tui keys file from an attrset of action → list of key names.
# Omitted actions keep the app defaults (same merge rules as a hand-written file).
{ lib }:
keys:
let
  actions = import ./actions.nix;
  unknown = lib.attrNames (lib.filterAttrs (name: _: !(lib.elem name actions)) keys);
in
assert lib.assertMsg (unknown == [ ]) "gmail-tui.keys: unknown action(s): ${lib.concatStringsSep ", " unknown}";
lib.concatStringsSep "\n" (
  [
    "# Managed by Nix — overrides only; omitted actions keep app defaults."
    "# Special keys: tab enter space backtab up down left right home end pageup pagedown"
    "# Alt: alt-l   Control: ctrl-o   Sequences: g-i   Case-sensitive."
    ""
  ]
  ++ lib.mapAttrsToList (
    action: bindings:
    assert lib.assertMsg (bindings != [ ]) "gmail-tui.keys.${action}: need at least one key";
    "${action} ${lib.concatStringsSep " " bindings}"
  ) keys
  ++ [ "" ]
)
