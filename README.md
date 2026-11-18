# momentum

Your [Momentum Grids](https://momentumgrids.com) habits and focus timers, in the terminal.

Start a timer for a tag, log today's habits, and see your day at a glance —
without leaving your editor. Everything syncs with the iPhone app and the
website: a timer you start here shows up on your phone, and the other way round.

## Install

```sh
brew install gokdenizinan/tap/momentum
```

Works on macOS and Linux. It's one Python file with no dependencies; macOS's
built-in Python is enough.

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

## Use it

Run `momentum` on its own to open the interactive view:

```
 Momentum Grids                               Tue 18 Nov
 ──────────────────────────────────────────────────────
 ● FIN 401 · Chapter 3                          23:41
   ███████████░░░░░░░░░  12:19 left

 Today  3 of 5 done
 ✓ 📚 Read
›· 💧 Water            3 / 8 glasses   ███░░░░░
 · 🏃 Run
```

| Key | What it does |
| --- | --- |
| ↑ ↓ (or k j) | Move between habits |
| Enter or Space | Mark done / undo, or add one step to a number habit |
| + and − | Add or subtract a step |
| t | Start a timer: type to find a tag, Enter, then minutes for a countdown (or Enter for none) |
| p | Pause or resume |
| s | Save the timer |
| d | Discard the timer (asks first) |
| r | Refresh |
| ? | Help |
| q | Quit — your timer keeps running |

Or use single commands:

```sh
momentum start "FIN 401"        # timer on a tag (part of the name is enough)
momentum start kairos -m 25     # 25-minute countdown
momentum status                 # what's running
momentum stop                   # save the timer
momentum habits                 # today's habits
momentum log reading            # mark a habit done
momentum log water +2           # add 2 to a number habit (-1 subtracts, 8 sets)
momentum undo water             # clear today's log
momentum --help                 # everything else
```

## Privacy and security

- Signing in creates a separate session for this computer. It appears in
  **Settings → Signed-in devices** on momentumgrids.com, where you can sign it
  out at any time. `momentum logout` does the same from the terminal (running
  timers are kept).
- Your login is saved in `~/.config/momentum-grids/session.json`, readable only
  by your user account. It is never sent anywhere except momentumgrids.com.
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
