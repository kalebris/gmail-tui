{
  description = "gmail-tui — online-first Gmail TUI (local index + body cache)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      mkPkgs =
        system:
        import nixpkgs {
          inherit system;
          overlays = [ self.overlays.default ];
        };
    in
    {
      overlays.default = final: prev: {
        gmail-tui = final.python3Packages.buildPythonApplication {
          pname = "gmail-tui";
          version = "0.1.0";
          pyproject = true;
          src = self;

          build-system = [ final.python3Packages.hatchling ];

          # notify-send for desktop alerts; terminfo for curses
          nativeBuildInputs = [ final.makeWrapper ];
          buildInputs = [ final.ncurses ];

          postInstall = ''
            wrapProgram $out/bin/gmail-tui \
              --prefix PATH : ${
                final.lib.makeBinPath [
                  final.libnotify
                  final.age
                  final.age-plugin-tpm
                ]
              } \
              --prefix TERMINFO_DIRS : ${final.ncurses}/share/terminfo
          '';

          meta = with final.lib; {
            description = "Online-first Gmail TUI with a local index and body cache";
            license = licenses.gpl3Only;
            mainProgram = "gmail-tui";
            platforms = platforms.unix;
          };
        };
      };

      packages = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
        in
        {
          default = pkgs.gmail-tui;
          gmail-tui = pkgs.gmail-tui;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/gmail-tui";
        };
      });

      # Portable shortcut config: programs.gmail-tui.keys = { archive = [ "e" ]; };
      nixosModules.default = import ./nix/nixos.nix;
      nixosModules.gmail-tui = self.nixosModules.default;

      homeModules.default = import ./nix/home-manager.nix;
      homeModules.gmail-tui = self.homeModules.default;

      # Alias used by some Home Manager layouts.
      homeManagerModules = self.homeModules;

      devShells = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              gmail-tui
              uv
              python3
              sqlite
              ncurses
              libnotify
              age
              age-plugin-tpm
            ];
            env.UV_PYTHON = "${pkgs.python3}/bin/python3";
            env.UV_PYTHON_DOWNLOADS = "never";
            shellHook = ''
              export TERMINFO_DIRS="${pkgs.ncurses}/share/terminfo''${TERMINFO_DIRS:+:''$TERMINFO_DIRS}"
            '';
          };
        }
      );
    };
}
