# Legacy entry: `nix-shell`. Prefer `nix develop` (flake.nix) when flakes are enabled.
{ pkgs ? import <nixpkgs> { } }:

pkgs.mkShell {
  packages = with pkgs; [
    uv
    python3
    sqlite
    ncurses
    libnotify
  ];

  # Use the nix python (ncurses-linked). Don't let uv fetch another interpreter.
  env.UV_PYTHON = "${pkgs.python3}/bin/python3";
  env.UV_PYTHON_DOWNLOADS = "never";

  shellHook = ''
    export TERMINFO_DIRS="${pkgs.ncurses}/share/terminfo''${TERMINFO_DIRS:+:''$TERMINFO_DIRS}"
    # Install this project into .venv so `gmail-tui` is a real entry-point binary.
    uv sync --quiet
    export PATH="$PWD/.venv/bin:$PATH"
  '';
}
