# momentum

Your [Momentum Grids](https://momentumgrids.com) habits, focus timers and tasks, in the terminal.

Start a timer for a tag, log today's habits, see your streaks and your whole
year, and check off tasks without leaving your editor. Everything syncs with
the iPhone app and the website: a timer you start here shows up on your phone,
and the other way round.

## Install

```sh
brew install gokdenizinan/tap/momentum
```

Works on macOS and Linux. It's one Python file with no dependencies; macOS's
built-in Python is enough. Homebrew also installs tab completion for zsh,
bash and fish.

## Sign in (once)

```sh
momentum login
```

You'll see a short code like `BCDF-GHJK`. Press Enter, and your browser opens
momentumgrids.com/cli. Sign in there if asked (email, Apple or Google all
work), type the code, check the device name, and click **Approve**. Your
terminal is now signed in.

> Only approve a code you just got from your own terminal. If someone sends you
> a code, deny it — approving lets their computer into your account.

Prefer email and password? `momentum login --password`.

## The interactive view

Run `momentum` on its own:

```
 Momentum Grids    1 Today  2 Grid  3 Focus  4 Tasks             Wed 18 Nov
 ─────────────────────────────────────────────────────────────────────────
 ● Problem set 4 · FIN 401                                          41:27
   ████████████████░░░░  08:33 left

 TODAY  3 of 6 done  ●●●○○○                  LAST 9 DAYS █▇█▇█▆██▄ 88%
   HABIT        LAST 14 DAYS   STREAK  TODAY
›✓ Water        ■■■■■■■■■■■■■■    23d  8 / 8 glasses  ████████
 ✓ Reading      ■■□■■■■■■■■■■■    11d  done
 ○ Run          ·■··■·□·■··■·□     2d  to do
 ❄ Guitar       ■■□■■■■■■■□■■❄     2d  frozen today
```

`■` done · `■` partly done · `□` missed · `❄` frozen · `·` not scheduled.

Four tabs, switched with `1`–`4`:

| Tab | What you see |
| --- | --- |
| 1 Today | Today's habits with their last 14 days and streaks, the running timer and a 9-day trend |
| 2 Grid | A habit's whole year, with done days, rate, best run and monthly bars. Move to any day and press Enter to fix it |
| 3 Focus | Your focus time for the last 7, 30 or 365 days, by day and by tag, plus recent sessions and CSV export |
| 4 Tasks | Your Kairos tasks for today, tomorrow and later |

Keys on Today:

| Key | What it does |
| --- | --- |
| ↑ ↓ (or k j) | Move between habits |
| Enter or Space | Mark done / undo, or add one step to a number habit |
| + and − | Add or subtract a step |
| g | The selected habit's last 16 weeks |
| f | Focus mode: a big clock for the running timer |
| t | Start a timer: type to find a tag, Enter, then minutes for a countdown (or Enter for none) |
| p / s / d | Pause or resume, save, discard (asks first) |
| r | Refresh |
| ? | All keys, including the other tabs |
| q | Quit — your timer keeps running |

When a countdown ends, you get a bell and, on a Mac, a notification
(`MOMENTUM_NOTIFY=0` turns it off).

## Single commands

```sh
momentum start "FIN 401"        # timer on a tag (part of the name is enough)
momentum start kairos -m 25     # 25-minute countdown
momentum status                 # what's running (add --watch for a live clock)
momentum stop                   # save the timer

momentum today                  # today's habits with history and streaks
momentum log reading            # mark a habit done
momentum log water +2           # add 2 to a number habit (-1 subtracts, 8 sets)
momentum undo water             # clear today's log
momentum grid reading           # last 16 weeks of one habit
momentum year reading           # the whole year (--year 2025 for another)

momentum week                   # focus time for the last 7 days
momentum export --days 30       # focus sessions as CSV (--output file.csv)

momentum tasks                  # Kairos tasks
momentum task add "Pay rent" --tomorrow
momentum task done rent

momentum --help                 # everything else
```

Most read commands take `--json` for scripts.

## In your prompt or tmux

`momentum prompt` prints a short status such as `● FIN 401 41:27 · 4/6`. It
reads a small cache and refreshes it at most once a minute, so it's fast and
never slows down or breaks your prompt.

```sh
# zsh (~/.zshrc)
setopt prompt_subst
RPROMPT='$(momentum prompt --color never)'
```

```toml
# Starship (~/.config/starship.toml): add ${custom.momentum} to your format
[custom.momentum]
command = "momentum prompt --color never"
when = "command -v momentum >/dev/null 2>&1"
format = "[$output]($style) "
style = "yellow"
```

```tmux
# tmux (~/.tmux.conf)
set -g status-right '#(momentum prompt --tmux --color always) | %H:%M'
```

## Colours

Colours follow your terminal's light or dark background automatically. Set
`MOMENTUM_THEME=light` or `dark` to choose, `NO_COLOR=1` or `--color never`
for none, and `MOMENTUM_ASCII=1` if your font lacks the symbols.

In Ghostty and other terminals whose terminfo can repeat characters, macOS's
older ncurses garbles repeated symbols, so the interactive view runs as
`xterm-256color` there (only for itself). `MOMENTUM_KEEP_TERM=1` keeps your
`TERM` unchanged.

## Privacy and security

- Signing in creates a separate session for this computer. It appears in
  **Settings → Signed-in devices** on momentumgrids.com, where you can sign it
  out at any time. `momentum logout` does the same from the terminal (running
  timers are kept).
- Your login is saved in `~/.config/momentum-grids/session.json`, readable only
  by your user account. It is never sent anywhere except momentumgrids.com.
  The prompt cache next to it holds only the running timer and today's count,
  never your login.
- Changing your password or signing out everywhere also signs out the terminal.

## Uninstall

```sh
momentum logout
brew uninstall momentum
rm -rf ~/.config/momentum-grids
```

## Help

Questions or problems: open an issue here, or contact us through
[momentumgrids.com](https://momentumgrids.com).
