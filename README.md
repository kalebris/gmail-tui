# gmail-tui

Online-first Gmail TUI: local metadata index, body cache, and background sync.
Python 3.11+, stdlib only (no third-party runtime deps).

This project is vibecoded (built with an AI coding assistant in the loop). Treat it
like any young tool: useful, opinionated, and still evolving.

Licensed under [GPL-3.0](LICENSE) (no third-party runtime libraries to conflict with that).

Mail and OAuth tokens live under XDG paths by default:

| What | Where |
| --- | --- |
| Config / key bindings | `~/.config/gmail-tui/` |
| Index, blobs, tokens | `~/.local/share/gmail-tui/accounts/<email>/` |
| Named profile store | `<data>/<profile>/accounts/<email>/` |

Override with `--config FILE_OR_DIR` (or `GMAIL_TUI_CONFIG`) when the XDG defaults
are a poor fit — a directory means `<dir>/config`, and keys default to `<dir>/keys`.
`--data DIR` still overrides the mail store from that file. `--profile NAME` (or
`GMAIL_TUI_PROFILE`) keeps that shared config and uses `<data>/NAME/` for the
index and tokens instead — handy for separate “home” / “work” mailboxes without
duplicating key bindings. To choose the top-bar order, add one `account` line per
address in `~/.config/gmail-tui/config`. The file order is left to right. A mailbox
you leave out still shows up, after the ones you named. Date stamps (mail list,
calendar, CLI) share strftime patterns from the same file:

```
account ada@gmail.com
account bea@gmail.com
date %Y-%m-%d
time %H:%M
datetime %Y-%m-%d %H:%M
weekday %a %d
```

If you previously used `gtui`, move or symlink the old dirs:

```bash
mv ~/.config/gtui ~/.config/gmail-tui
mv ~/.local/share/gtui ~/.local/share/gmail-tui
```

## Google OAuth client (required once)

gmail-tui does **not** ship Google credentials. Create your own Desktop OAuth client:

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create (or pick) a project.
2. Enable the **Gmail API** and the **Google Calendar API**.
3. Configure the OAuth consent screen (External is fine). While the app is in **Testing**, add your Google account as a test user.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
5. Application type: **Desktop app**. Create it, then **Download JSON**.
6. Keep that file out of git (this repo ignores `client_secret.json` and `client_secret_*.json`). Store it somewhere private, e.g. `~/.config/gmail-tui/client_secret.json`.

Sign in:

```bash
gmail-tui login --client-json ~/.config/gmail-tui/client_secret.json
```

Alternatives: `--client-id` / `--client-secret`, or env vars `GMAIL_TUI_CLIENT_ID` and `GMAIL_TUI_CLIENT_SECRET`.
After the first login, tokens (and the client id/secret) are stored as `oauth.json` (`0600`) in that account’s folder under `accounts/<email>/`.

Login asks for Gmail modify plus full Calendar access (list calendars, view/create/edit/delete events). Pick the Google account in the browser; gmail-tui stores the tokens on `accounts/<email>/` for that address. Older installs that kept the first mailbox in the data root are moved into `accounts/` automatically. If you logged in before Calendar support, run `gmail-tui login` again for each account you use (a token refresh does not add Calendar). Also enable **Google Calendar API** on the same Cloud project as your OAuth client — a 403 “API has not been used” means that switch is still off.

Google may show a “This app hasn’t been verified” warning for your unpublished client — that is expected until you publish and verify the consent screen.

## Nix flake (use as a package)

Add this repo as a flake input and put `gmail-tui` on your PATH.

### NixOS config

```nix
# in your flake.nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    gmail-tui.url = "github:YOUR_USER/gmail-tui"; # git+ssh://… or path:./gmail-tui also fine
  };

  outputs = { self, nixpkgs, gmail-tui, ... }: {
    nixosConfigurations.you = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        gmail-tui.nixosModules.default
        ({ pkgs, ... }: {
          nixpkgs.overlays = [ gmail-tui.overlays.default ];

          programs.gmail-tui = {
            enable = true;
            # Portable shortcuts: written to the Nix store and selected via
            # GMAIL_TUI_KEYS. Only list overrides; other actions keep defaults.
            keys = {
              archive = [ "e" ];
              down = [ "j" "down" ];
              inbox = [ "g-i" ];
            };
            # Or ship a full file: keysFile = ./gmail-tui-keys;
          };
        })
      ];
    };
  };
}
```

Package-only (no module / no managed keys):

```nix
({ pkgs, ... }: {
  nixpkgs.overlays = [ gmail-tui.overlays.default ];
  environment.systemPackages = [ pkgs.gmail-tui ];
})
```

### Home Manager

```nix
{ config, inputs, pkgs, ... }:
{
  imports = [ inputs.gmail-tui.homeModules.default ];
  nixpkgs.overlays = [ inputs.gmail-tui.overlays.default ];

  programs.gmail-tui = {
    enable = true;
    # Writes data … into ~/.config/gmail-tui/config (mail store root).
    storeDir = "${config.home.homeDirectory}/mail/gmail-tui";
    # Shell aliases: gmail-tui-work → gmail-tui --profile work
    # (shared config/keys; separate store under <storeDir>/work/).
    profiles = [ "work" "home" ];
    # Extra lines for the managed config (optional).
    # extraConfig = ''
    #   account ada@example.com
    # '';
    # Writes ~/.config/gmail-tui/keys (overrides only).
    keys = {
      archive = [ "e" ];
      down = [ "j" "down" ];
      inbox = [ "g-i" ];
    };
    # Or ship a full file: keysFile = ./gmail-tui-keys;
  };
}
```

You can also point any install at a keys file with `GMAIL_TUI_KEYS=/path/to/keys` or `gmail-tui --keys ./keys`.

One-off without editing your system flake:

```bash
nix run github:YOUR_USER/gmail-tui -- --help
nix profile install github:YOUR_USER/gmail-tui
```

Local checkout:

```bash
nix build .#gmail-tui    # ./result/bin/gmail-tui
nix run . -- login --client-json ~/.config/gmail-tui/client_secret.json
```

### Dev shell (optional)

For hacking on this repo (`uv`, editable workflow):

```bash
nix develop          # flake
nix-shell            # shell.nix, no flakes
```

## Install without Nix

System packages you may want: Python ≥ 3.11, SQLite, a terminal with decent Unicode (sixel optional), and `libnotify` / `notify-send` for desktop new-mail alerts.

With [uv](https://github.com/astral-sh/uv):

```bash
uv sync
source .venv/bin/activate   # or: uv run gmail-tui …
gmail-tui login --client-json ~/.config/gmail-tui/client_secret.json
```

Or editable pip:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Quick usage

```bash
gmail-tui                 # interactive TUI
gmail-tui sync            # refresh local index / bodies
gmail-tui list            # print local index
gmail-tui search QUERY
gmail-tui open GMAIL_ID
gmail-tui login --client-json …        # again for another Google account (or add-account)
```

## Keyboard shortcuts

Press `?` in the TUI for the same list, filtered to the focused pane. Keys are
case-sensitive. Sequences use a hyphen (`g-i` = `g` then `i`). Override bindings
in `~/.config/gmail-tui/keys` (or via Nix `programs.gmail-tui.keys`).

### Defaults (all actions)

| Action | Keys |
| --- | --- |
| quit | `q` `Q` |
| sync | `s` |
| sync-all | `S` |
| cache (pause/resume body downloads) | `B` |
| snooze | `b` |
| down | `j` `↓` |
| up | `k` `↑` |
| page-down | `space` `PgDn` |
| page-up | `PgUp` |
| top | `g` `Home` |
| bottom | `G` `End` |
| inbox | `g-i` |
| left | `h` `←` |
| right | `→` |
| open (fold/expand) | `z-o` |
| close (fold/collapse) | `z-c` |
| toggle-fold | `z-a` |
| activate | `Enter` |
| next-pane | `Tab` |
| prev-pane | `Backtab` |
| search | `/` |
| add-label | `l` |
| archive | `a` `e` |
| delete | `d` |
| select | `x` |
| order (newest/oldest) | `o` |
| toggle-unread | `u` |
| next-unread | `n` |
| compose | `c` |
| calendar | `C` |
| reply-all | `r` |
| reply | `R` |
| holding (outbox) | `H` |
| attachments | `A` |
| mime-parts | `m` |
| view-image | `v` |
| undo | `U` |
| next-account | `]` |
| prev-account | `[` |
| help | `?` |

### By context

Help (`?`) shows only what applies to the focused pane. Shared actions at the
bottom of each list: next/prev pane, next/prev account, sync, sync-all, cache,
snooze, inbox, help.

**Folders** (left pane)

| Keys | Does |
| --- | --- |
| `j`/`k`, arrows, PgUp/PgDn, `g`/`G` | move in the folder tree |
| `→` / `z-o` | expand a folder, or open the mail list |
| `←` / `z-c` | collapse a folder |
| `z-a` | toggle folder fold |
| `Enter` | list that folder’s mail |
| `/` | filter folders |
| `c` | write a new message in another window |
| `C` | open the calendar |
| `q` | quit (clears an active search first) |

**Mail list**

| Keys | Does |
| --- | --- |
| `j`/`k`, arrows, PgUp/PgDn, `g`/`G` | move in the list |
| `Enter` / `→` | read the message (or open a thread) |
| `←` / `z-c` | close a thread, or go to folders |
| `z-o` / `z-a` | open / toggle a thread |
| `/` | search mail |
| `x` | mark mail, then move down |
| `a`/`e` | archive |
| `b` | snooze |
| `d` | delete |
| `l` | add or remove a label |
| `u` / `n` | toggle unread / jump to next unread |
| `o` | newest or oldest first |
| `c` | write a new message in another window |
| `C` | open the calendar |
| `r` / `R` | reply-all / reply |
| `A` / `m` | attachments / MIME parts |
| `H` | mail waiting to be sent |
| `U` | undo |
| `q` | quit (clears an active search first) |

**Message reader**

| Keys | Does |
| --- | --- |
| `j`/`k`, space/PgDn, PgUp, `g`/`G` | scroll the body |
| `q` / `←` | back to the mail list |
| `/` | find in this message |
| `c` | write a new message in another window |
| `C` | open the calendar |
| `r` / `R` | reply-all / reply |
| `A` / `m` / `v` | attachments / MIME parts / show picture |
| `a`/`e`, `b`, `d`, `l`, `u`, `n`, `U`, `H` | archive, snooze, delete, label, unread, undo, holding |

`c`, `r`, and `R` open `$EDITOR` in a new terminal (`GMAIL_TUI_TERMINAL` is that command; otherwise kitty, foot, alacritty, and the other common terminals). This window keeps running, so other mail can be read while the message is written. When the editor exits, a dialog offers Send or Save as draft. Drag with the mouse to highlight text; the selection is copied.

**Calendar** (`C`)

Left: every account’s calendars in a tree; `Enter` toggles which ones are shown. Right: agenda for the current day, week, or month.

| Keys | Does |
| --- | --- |
| `C` / `q` / `Esc` | back to mail |
| `Tab` | calendars ↔ agenda |
| `D` / `W` / `M` | day / week / month |
| `n` / `p` (or PgDn/PgUp) | next / previous range |
| `t` | jump to today |
| `c` | new event (editor in another window) |
| `e` / `Enter` | edit the selected event |
| `d` | delete the selected event |
| `j`/`k`, `→`/`←` | move / expand |

Event drafts are plain text (`Title`, `When`, `Until`, `Kind: timed|all-day`, …).

**Attachments**

| Keys | Does |
| --- | --- |
| `j`/`k` | next / previous file |
| `Enter` | save or open the file |
| `v` | show the selected picture |
| `Esc` | back to the message |
| `q` / `←` | back to the mail list |
| `c`, `r`/`R`, `a`/`e`, `b`, `d`, `U` | compose, reply, archive, snooze, delete, undo |

**MIME parts** (`m`)

| Keys | Does |
| --- | --- |
| `j`/`k` | next / previous part |
| `Enter` | open HTML in the browser (`xdg-open` on a temp file) |
| `Esc` | back to the message |
| `q` / `←` | back to the mail list |
| `A` | list attachments |
| `c`, `r`/`R`, `a`/`e`, `b`, `d`, `U` | compose, reply, archive, snooze, delete, undo |

**Snooze prompt** (`b`): `j`/`k` pick a preset, or type a duration like `30m` / `2h` / `3d`, `Enter` confirms, `Esc` cancels.
