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
duplicating key bindings. `profiles/<name>` next to the config file can override
`open` for that profile. To choose the top-bar order, add one `account` line per
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
# signature ~/.config/gmail-tui/signature
# signature ada@gmail.com ~/.config/gmail-tui/signature-ada
```

`signature` is a file. Its text is placed in a new message, and in a reply above the quoted original. The file is read when the editor opens. A bare path is the default for every account; `signature user@host PATH` sets that mailbox only. `~/.config/gmail-tui/profiles/<name>` (beside `config`) may set the same lines for `--profile <name>`. Lookup order: profile account → profile default → shared account → shared default.

Links and HTML parts open with `xdg-open`. Set `open` to use another program; the URL or HTML file is added as the last argument:

```
open firefox -P settings
```

That line applies to every profile. The same `profiles/<name>` file overrides `open` and `signature` for that profile. Home Manager writes both from `programs.gmail-tui.settings` and `programs.gmail-tui.profiles.<name>`. The word `link` stays an OSC 8 hyperlink, so the terminal's URL mode can list it (foot: Ctrl+Shift+O). `O` opens the next link with the `open` command. Ctrl+O does the same job inside gmail-tui: each visible `link` gets a jump label, and the letter you press opens that address with the `open` command. A click on that word uses the command when it is not `xdg-open`. HTML parts use the same command. Saved attachments stay on `xdg-open`.

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

### TPM-sealed tokens (optional)

To keep refresh tokens off disk in plaintext, enable TPM sealing in
`~/.config/gmail-tui/config`:

```
oauth-tpm yes
# oauth-tpm-identity ~/.config/gmail-tui/oauth-tpm.identity
```

gmail-tui then stores `oauth.json.age` encrypted with [age](https://age-encryption.org/)
and [age-plugin-tpm](https://github.com/Foxboron/age-plugin-tpm). On first use it
creates a TPM-bound identity (you choose a PIN). Every later start asks for that
PIN once, decrypts the tokens into memory, and never re-prompts for refreshes in
the same process. Plaintext `oauth.json` files are migrated automatically the
first time they are read with `oauth-tpm` on.

Needs a TPM, plus `age` and `age-plugin-tpm` on `PATH` (the Nix package wraps
both). Tokens stay bound to that machine’s TPM — copy the mail cache freely, but
run `gmail-tui login` again on a new host. For non-interactive use, set
`AGE_TPM_PIN` instead of the prompt.

Login asks for Gmail modify plus full Calendar access (list calendars, view/create/edit/delete events). Pick the Google account in the browser; gmail-tui stores the tokens on `accounts/<email>/` for that address. Older installs that kept the first mailbox in the data root are moved into `accounts/` automatically. If you logged in before Calendar support, run `gmail-tui login` again for each account you use (a token refresh does not add Calendar). Also enable **Google Calendar API** on the same Cloud project as your OAuth client — a 403 “API has not been used” means that switch is still off.

Google may show a “This app hasn’t been verified” warning for your unpublished client — that is expected until you publish and verify the consent screen.

## Nix flake

Flake URL: [`github:kalebris/gmail-tui`](https://github.com/kalebris/gmail-tui)
(`git+ssh://…` or `path:./gmail-tui` also work). Outputs:

| Output | Use |
| --- | --- |
| `overlays.default` | Adds `pkgs.gmail-tui` |
| `packages.<system>.default` | The package |
| `homeModules.default` | Home Manager: `programs.gmail-tui` |
| `nixosModules.default` | NixOS: `programs.gmail-tui` (keys via `GMAIL_TUI_KEYS`) |

### Home Manager (recommended)

Import the module **and** apply the overlay (required when Home Manager does not
use the system `pkgs`, e.g. `useGlobalPkgs = false`).

```nix
# flake.nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    home-manager.url = "github:nix-community/home-manager";
    gmail-tui.url = "github:kalebris/gmail-tui";
  };
  # … wire inputs.gmail-tui into your HM user module as usual …
}
```

```nix
# homes/you/gmail-tui.nix (or inline in the HM user module)
{ config, inputs, ... }:
{
  imports = [
    inputs.gmail-tui.homeModules.default
    # Optional: decrypt OAuth client JSON with agenix (home module).
    inputs.agenix.homeManagerModules.default
  ];
  nixpkgs.overlays = [ inputs.gmail-tui.overlays.default ];

  age.secrets.gmail-tui-client.file = ../../secrets/gmail-tui-client.age;

  programs.gmail-tui = {
    enable = true;

    # Shared ~/.config/gmail-tui/config. Unset options keep the app default.
    settings = {
      # data = "${config.home.homeDirectory}/mail/gmail-tui";
      # accounts = [ "ada@example.com" "bea@example.com" ];
      # download = "~/Downloads";
      # open = [ "xdg-open" ];
      # signature = "~/.config/gmail-tui/signature";
      # signatures."ada@example.com" = "~/.config/gmail-tui/signature-ada";
    };

    # PATH wrappers: gmail-tui-work / gmail-tui-home.
    # open / signature(s) write profiles/<name> next to the config.
    # A list of names still works: profiles = [ "work" "home" ];
    profiles = {
      work = {
        open = [ "firefox" "-P" "settings" ];
        # signature = "~/.config/gmail-tui/signature-work";
        # signatures."ada@work.example" = "~/.config/gmail-tui/sig-work-ada";
      };
      home = { };
    };

    # Decrypted Google Desktop OAuth client JSON (string path — not a Nix path).
    # Installs gmail-tui-login and gmail-tui-<profile>-login on PATH.
    clientJson = config.age.secrets.gmail-tui-client.path;

    # Optional key overrides → ~/.config/gmail-tui/keys
    # keys = { archive = [ "e" ]; };
    # Or a full file: keysFile = ./gmail-tui-keys;
  };
}
```

After `home-manager switch` / `nixos-rebuild switch` (with HM as a NixOS
module), you get binaries on `PATH`:

```bash
gmail-tui                 # default store
gmail-tui-work            # → gmail-tui --profile work
gmail-tui-home            # → gmail-tui --profile home
gmail-tui-login           # → gmail-tui login --client-json <clientJson>
gmail-tui-work-login      # → gmail-tui --profile work login --client-json …
gmail-tui-home-login      # → gmail-tui --profile home login --client-json …
```

Same as running `gmail-tui --profile work` yourself. Login once per profile
(tokens live under that profile’s store). `clientJson` is optional; without it
only the profile wrappers are installed.

### NixOS (system package + keys)

```nix
# in your flake.nix
{
  inputs.gmail-tui.url = "github:kalebris/gmail-tui";

  outputs = { nixpkgs, gmail-tui, ... }: {
    nixosConfigurations.you = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        gmail-tui.nixosModules.default
        ({ pkgs, ... }: {
          nixpkgs.overlays = [ gmail-tui.overlays.default ];
          programs.gmail-tui = {
            enable = true;
            # Portable shortcuts in the Nix store; selected via GMAIL_TUI_KEYS.
            keys = {
              archive = [ "e" ];
              down = [ "j" "down" ];
              inbox = [ "g-i" ];
            };
            # Or: keysFile = ./gmail-tui-keys;
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

You can also point any install at a keys file with `GMAIL_TUI_KEYS=/path/to/keys`
or `gmail-tui --keys ./keys`.

### One-off / local checkout

```bash
nix run github:kalebris/gmail-tui -- --help
nix profile install github:kalebris/gmail-tui

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
gmail-tui                 # interactive TUI (default store)
gmail-tui-work            # same, with --profile work (Home Manager profiles=)
gmail-tui-home            # same, with --profile home
gmail-tui-login           # OAuth login using programs.gmail-tui.clientJson
gmail-tui-work-login      # login into the work profile store
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
| page-down | `J` `space` `PgDn` |
| page-up | `K` `PgUp` |
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
| raw-source (whole message as saved) | `V` |
| headers (all header fields / short set) | `F` |
| urls (hide / show link addresses) | `L` |
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
| `c` | address a new message, then edit it |
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
| `o` | newest or oldest first (remembered under the data dir) |
| `c` | address a new message, then edit it |
| `C` | open the calendar |
| `r` / `R` | reply-all / reply |
| `A` / `m` | attachments / MIME parts |
| `V` / `F` | raw source / all header fields of the selected mail |
| `L` | hide or show link addresses in the reader |
| `H` | mail waiting to be sent |
| `U` | undo |
| `q` | quit (clears an active search first) |

**Message reader**

| Keys | Does |
| --- | --- |
| `j`/`k`, space/PgDn, PgUp, `g`/`G` | scroll the body |
| `q` / `←` | back to the mail list |
| `/` | find in this message |
| `c` | address a new message, then edit it |
| `C` | open the calendar |
| `r` / `R` | reply-all / reply |
| `A` / `m` / `v` | attachments / MIME parts / show picture |
| `V` | raw message source (every header, MIME boundary, encoded part); `V` or `Esc` returns |
| `F` | show all header fields, or back to From/To/Date/Subject (stays on for later messages) |
| `L` | hide link addresses: only the word `link` stays; `L` again shows them |
| `O` | open the next link (`open` in the config, or `xdg-open`) |
| `a`/`e`, `b`, `d`, `l`, `u`, `n`, `U`, `H` | archive, snooze, delete, label, unread, undo, holding |

`c` opens a To, Cc, and Bcc dialog first. Tab completes the address being typed from the From and To headers of mail already stored. Enter then opens `$EDITOR` in a new terminal (`GMAIL_TUI_TERMINAL` is that command; otherwise kitty, foot, alacritty, and the other common terminals). `r` and `R` open the editor directly. The signature file for the active profile and account is already in the draft, above any quoted text. This window keeps running, so other mail can be read while the message is written. When the editor exits, a dialog offers Send or Save as draft. Drag with the mouse to highlight text; the selection is copied.

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
| `Enter` | open HTML with the `open` command (`xdg-open` on a temp file by default) |
| `Esc` | back to the message |
| `q` / `←` | back to the mail list |
| `A` | list attachments |
| `c`, `r`/`R`, `a`/`e`, `b`, `d`, `U` | compose, reply, archive, snooze, delete, undo |

**Snooze prompt** (`b`): `j`/`k` pick a preset, or type a duration like `30m` / `2h` / `3d`, `Enter` confirms, `Esc` cancels.
