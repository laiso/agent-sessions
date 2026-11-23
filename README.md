# agent-sessions

List Herdr and tmux panes alongside Orca bots—showing agent, status, foreground command, and working directory—and send messages to panes.

Run the launcher from the repository with Python 3.11 or later. No Python packages are required.

```sh
./agent-sessions
./agent-sessions --source herdr --agents-only
./agent-sessions --source tmux --format json
./agent-sessions --source orca
./agent-sessions --no-header --width 100
```

Example output:

```text
SOURCE  SESSION  PANE  AGENT  STATUS   COMMAND          CWD
herdr   my-app   1     agy    idle     agy              ~/work/my-app
tmux    demo     0.0   -      running  sleep 300        ~/work/demo
orca    helper   home  codex  idle     Code review      ~/work/helper
```

| Option | Description |
|---|---|
| `--source all\|herdr\|tmux\|orca` | Sources to read (`all` queries Herdr, tmux, then Orca). Defaults to `all`. |
| `--format table\|json` | Defaults to a table. JSON also includes pane IDs, focus state, and full paths. |
| `--agents-only` | Show panes with an identified agent and all Orca bots. |
| `--no-header` | Omit the table header. |
| `--width N` | Table width (untruncated when piped). |

Herdr reports the agent’s internal status. In tmux, `running` indicates a non-shell foreground process, `shell` a shell prompt, and `dead` a dead pane.

Each Orca row represents one bot. SESSION is its name, PANE is `home`, COMMAND is its specialty, and CWD is its home worktree. A paused bot shows `paused`; a home with no agents shows `stopped`. Bots remain visible when their home cannot be read. JSON includes `specialty`, which is null for other sources and for bots without a specialty.

Listing exits with `0` when at least one source can be read, `2` for invalid arguments, or `3` when all requested sources fail. A missing tmux server counts as an empty result. With `--source orca` alone, an unreadable bot list produces an empty result and exits silently with `0`. With `--source all`, an unreadable Orca source does not count as a success. If Herdr and tmux also fail, the exit code is `3`.

## Platforms and Orca discovery

Linux is the primary platform. Linux reads tmux process arguments from `/proc`. On macOS, one `ps` snapshot per collection supplies the newest direct child’s command, or the pane process’s own command. Both platforms fall back to the tmux command name when process details are unavailable. macOS reports arguments as one string, so agent identification is less precise for paths containing spaces.

Set `AGENT_SESSIONS_ORCA` to select a CLI explicitly. It takes precedence over `ORCA_CLI_COMMAND`. Otherwise the launcher checks only these known locations, in order, without searching PATH:

| Platform | Registered shim | Application, in search order |
|---|---|---|
| Linux | `${XDG_CONFIG_HOME:-$HOME/.config}/orca/linux-orca-cli-shim/orca` | `~/.local/opt/orca/orca.AppImage` |
| macOS | `~/.local/bin/orca` (Orca Settings → General → Orca CLI) | `/Applications/Orca.app/Contents/Resources/bin/orca`, `~/Applications/Orca.app/Contents/Resources/bin/orca`, then the legacy paths `/Applications/Orca Desktop.app/Contents/MacOS/orca` and `~/Applications/Orca Desktop.app/Contents/MacOS/orca` (exact lowercase filename required) |

Automatic candidates must resolve to executable regular files owned by the current user or root, without group or other write permission. If the registered shim reports `Orca CLI is unavailable`, the launcher retries that query once with the first eligible application. The only Orca queries are `bot list --json` and `worktree ps --json`, with a 15-second timeout each.

## Send a message

Preview the target and message with `--dry-run`. By default, `send` follows the message with Enter, which can execute a command at a shell prompt. Use `--no-enter` to leave the message unsubmitted.

```sh
./agent-sessions send --dry-run herdr:my-app/1 'How is the build going?'
./agent-sessions send herdr:w1:p1 'How is the build going?'
./agent-sessions send tmux:demo echo hello
./agent-sessions send --no-enter tmux:demo:0.0 -- '--help'
./agent-sessions send --dry-run --format json tmux:%12 'What is next?'
```

```text
Would send to herdr my-app/1 (w1:p1) [agy, idle]
> How is the build going? ⏎
```

Use `herdr:SESSION/PANE` or `herdr:PANE_ID` for Herdr. Use `tmux:SESSION` for the active pane, `tmux:SESSION:WINDOW.PANE` for an indexed pane, or `tmux:%ID` for a pane ID. Only name-based targets can omit the source prefix. IDs such as `w1:p1` and `%12` require it. Ambiguous matches print the candidates to stderr. Orca bots are list-only.

Message arguments are joined with one space. Put `--` before a message starting with a hyphen. Messages must contain non-whitespace text and may contain up to 4096 characters. Tabs, newlines and other control characters are rejected.

| Send exit code | Meaning |
|---|---|
| 0 | Sent or previewed |
| 2 | Invalid arguments or message |
| 3 | All requested sources failed |
| 4 | Target missing or dead pane |
| 5 | Ambiguous target |
| 6 | Delivery failed |

## Tests

Run these commands from the repository root:

```sh
python3 -m unittest discover -s tests
./run-e2e-tests.sh
E2E_REQUIRE_ALL=1 ./run-e2e-tests.sh
```

The live suite requires Herdr with at least one pane, tmux, and a running Orca installation whose application binary at a known location can list at least one bot. The macOS bundle discovery test (E9) also accepts an empty bot list and checks both CLI queries. Missing prerequisites skip the affected tests. Strict mode fails on missing prerequisites and allows skips for scenarios specific to another OS.

Live tests send no input to existing Herdr panes or Orca bots. Herdr is read or previewed with `send --dry-run`; Orca is queried read-only. Actual sends go only to a disposable pane on an isolated tmux server, which the suite removes afterward.

macOS Orca discovery has been checked with platform-substituted unit tests only, and live validation on a Mac is still pending. The macOS `ps` parsing also runs in the live suite against the real process table, because Linux procps accepts the same `ps` options. The [design specification](docs/design.md) is in Japanese.
