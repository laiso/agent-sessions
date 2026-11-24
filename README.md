# agent-sessions

List Herdr and tmux panes alongside Orca bots—showing agent, status, foreground command, and working directory—and read their output or send messages to panes.

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

Automatic candidates must resolve to executable regular files owned by the current user or root, without group or other write permission. If the registered shim reports `Orca CLI is unavailable`, the launcher retries that query once with the first eligible application. Listing uses `bot list --json` and `worktree ps --json`, with a 15-second timeout each.

## Read a session

`read` captures terminal output or reads an Orca bot’s messages without marking them read.
Use the same pane addresses as for `send`, including dead panes. An unprefixed name searches
Herdr and tmux. An Orca bot requires the explicit `orca:` prefix.

```sh
./agent-sessions read herdr:my-app/1
./agent-sessions read --limit 10 tmux:demo
./agent-sessions read --format json --limit 2 orca:helper
```

The default table displays a timestamp, message type, participants, optional ID and subject,
and indented body. It strips terminal escapes and control characters from the body except
newlines and tabs. JSON preserves the original text. The limit ranges from 1 to 1000.
For terminals it counts lines (default 50) after trailing empty lines are removed.
Each capture produces one record, even for an empty pane. For Orca it counts messages
(default 20), returning the most recent matches in chronological order.

All sources use the same JSON fields. Use `structured` (or `type`) to tell the two kinds apart:

- `structured: true` — an Orca message. `type` is Orca's message type (`handoff`, `question`,
  `status`, `escalation`, `worker_done`, …) and `from`, `to` and `subject` are set.
- `structured: false` — raw terminal text from Herdr or tmux. `type` is always
  `terminal-output`, `body` is the captured text as-is, and `id`, `to`, `subject` and
  `thread_id` are null. Interpret it yourself; it carries no message semantics.

These examples show a structured Orca message and a raw terminal capture from two separate reads.

```json
[
  {
    "source": "orca",
    "target": "orca:helper",
    "id": "msg_example",
    "type": "question",
    "structured": true,
    "from": "bot:reviewer",
    "to": "bot:helper",
    "subject": "Test results",
    "body": "Did the tests pass?",
    "timestamp": "2026-01-01T09:00:00Z",
    "thread_id": null
  }
]
```

```json
[
  {
    "source": "tmux",
    "target": "tmux:demo:0.0",
    "id": null,
    "type": "terminal-output",
    "structured": false,
    "from": "tmux:demo:0.0",
    "to": null,
    "subject": null,
    "body": "Tests passed.\nReady.",
    "timestamp": "2026-01-01T09:01:00Z",
    "thread_id": null
  }
]
```

Orca reads the bot list, all bots’ chat lists, and the latest 1000 messages across mailboxes.
A message matches when either participant is a chat of the requested bot or a run that bot
coordinated. A dispatch from one of its chats identifies the coordinator. Participants with a
known chat or run appear as `bot:NAME`. Other handles appear verbatim. Mail addressed to a run
whose bot has never dispatched a worker cannot be attributed through that run. Messages older
than the 1000-message inbox window are unavailable. An empty mailbox produces `[]` in JSON.

| Read exit code | Meaning |
|---|---|
| 0 | Read succeeded, including empty results |
| 2 | Invalid arguments |
| 3 | Requested source unavailable, including an unreadable Orca inbox |
| 4 | No matching target |
| 5 | Ambiguous target |
| 6 | Resolved pane could not be captured |

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

Use `herdr:SESSION/PANE` or `herdr:PANE_ID` for Herdr. Use `tmux:SESSION` for the active pane, `tmux:SESSION:WINDOW.PANE` for an indexed pane, or `tmux:%ID` for a pane ID. Only name-based targets can omit the source prefix. IDs such as `w1:p1` and `%12` require it. Ambiguous matches print the candidates to stderr. Use an Orca bot as a target for `read`.

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

The live suite requires Herdr with at least one pane, tmux, and a running Orca installation whose application binary at a known location can list at least one bot. The macOS bundle discovery test (E9) checks that the bundled CLI runs `bot list --json` once. An empty list completes E9 successfully. A nonempty list must be followed by one `worktree ps --json` query. The read tests also require a Herdr pane with output and a `claude` bot with inbox messages. They compare the read flag for each returned message before and after the read. Missing prerequisites skip the affected tests. Strict mode fails on missing prerequisites and allows skips for scenarios specific to another OS.

Live tests send no input to existing Herdr panes or Orca bots. Herdr is read or previewed with `send --dry-run`; Orca is queried read-only. Actual sends go only to a disposable pane on an isolated tmux server, which the suite removes afterward.

macOS Orca discovery has been validated live on a Mac (E9 passes; the bundled CLI is found and invoked). The macOS `ps` parsing also runs in the live suite against the real process table, because Linux procps accepts the same `ps` options.
