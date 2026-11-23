# agent-sessions 設計書 v5（v1 ＋ send ＋ Orca ソース ＋ E2E テスト ＋ macOS 対応）

作成: claude（設計・レビュー・テスト担当）／実装: codex

## 1. 目的

Herdr と tmux で動いているエージェントのセッション（ペイン）を一覧し、状態をひと目で分かる表にする CLI。

## 2. 前提と技術選定

- 言語: **Python 3.11 以上、標準ライブラリのみ**（依存ゼロ）。開発機の Python は 3.14。
  - 理由: 外部コマンドの JSON・テキストを読んで表にするだけなので、ビルドが要らず、テストもしやすい。
- テスト: `unittest`（標準）。`python3 -m unittest discover -s tests` で全件実行できること。
- 配置: `src/agent_sessions/`（src レイアウト）。`pyproject.toml` に console script `agent-sessions` を定義。
  リポジトリ直下の `./agent-sessions` シェルスクリプトからも、インストールなしで実行できるようにする（`PYTHONPATH=src python3 -m agent_sessions "$@"`）。

## 3. コマンドの形

```
agent-sessions [--source {all,herdr,tmux,orca}] [--format {table,json}]
               [--agents-only] [--no-header] [--width N]
```

| オプション | 既定 | 意味 |
|---|---|---|
| `--source` | `all` | 読むソース。`all` は herdr → tmux → orca の順 |
| `--format` | `table` | `table`: 人が読む表、`json`: 機械向けの JSON 配列 |
| `--agents-only` | オフ | エージェントが動いている行だけにする（herdr は `agent` があるペイン、tmux は §5.3 の判定で agent が付いたペイン、orca は全ボット） |
| `--no-header` | オフ | 表の見出し行を出さない |
| `--width N` | 端末幅 | 表の全体幅。未指定なら `shutil.get_terminal_size().columns`、端末でないとき（パイプ）は切り詰めない |

### 終了コード

- `0`: 指定したソースのうち少なくとも1つが読めた（0件でも 0）
- `2`: 引数の誤り（argparse の既定のまま）
- `3`: 指定したソースがすべて読めなかった（コマンドがない、サーバーが動いていない等）

読めなかったソースは stderr に `agent-sessions: herdr: <理由>` の形で1行警告を出し、残りのソースは表示を続ける。
Orca の取得失敗は §11.6 の扱いに従う。tmux の「サーバーが動いていない」（`no server running`）は **エラーではなく 0 件**として扱う（警告も出さない）。

## 4. 出力形式

### 4.1 表（既定）

列は次の順。左寄せ、列の間は空白2つ。

| 列 | 内容 |
|---|---|
| `SOURCE` | `herdr` / `tmux` / `orca` |
| `SESSION` | herdr: ワークスペースのラベル／tmux: セッション名／orca: ボット名 |
| `PANE` | §5 のペイン名。orca は `home` |
| `AGENT` | エージェント名（なければ `-`） |
| `STATUS` | §6 の状態 |
| `COMMAND` | 前面で動いているコマンドライン。orca は専門分野 |
| `CWD` | 作業ディレクトリ。ホーム配下は `~` に短縮 |

例:

```
SOURCE  SESSION       PANE    AGENT  STATUS   COMMAND                            CWD
herdr   wsh-2027-pre  1       agy    idle     agy --dangerously-skip-permissions ~/Work/laiso/wsh-2027-pre
herdr   wsh-2027-pre  2       -      unknown  /usr/bin/bash                      ~/Work/laiso/wsh-2027-pre
tmux    dev           0.1     claude working  claude                             ~/src/app
```

- 並び順: SOURCE（herdr → tmux → orca）、SESSION、PANE の順（herdr はワークスペース番号・タブ番号の順、tmux はウィンドウ番号・ペイン番号の数値順）。
- 0 件のとき: 見出しだけでなく、stderr ではなく stdout に `No sessions found.` を1行出す（`--format json` のときは `[]`）。
- 幅の調整: 全体幅が `--width`（または端末幅）を超える場合、**COMMAND → CWD の順に**右端を `…` で切り詰める。各列の最小幅は 8。その他の列は切り詰めない。
- 文字幅: 日本語など全角文字は幅2として数える（`unicodedata.east_asian_width` が `W`/`F` なら 2）。
- 色: v1 では付けない。

### 4.2 JSON

`--format json` は次のオブジェクトの配列を stdout に出す（インデント2、`ensure_ascii=False`）。

```json
{
  "source": "herdr",
  "session": "wsh-2027-pre",
  "pane": "1",
  "pane_id": "wN:pH",
  "agent": "agy",
  "status": "idle",
  "command": "agy --dangerously-skip-permissions",
  "cwd": "/home/kk/Work/laiso/wsh-2027-pre",
  "focused": false,
  "specialty": null
}
```

- `agent`・`command` がない場合は `null`。`cwd` は JSON では `~` に短縮しない。
- tmux の `pane_id` は tmux の `%12` 形式。

## 5. ソースの読み方

### 5.1 共通

- 外部コマンドは `subprocess.run(argv, capture_output=True, text=True, timeout=5)` で実行する。シェルは使わない。
- コマンド実行は `Runner`（`argv: list[str] -> CompletedProcess` を返す呼び出し可能オブジェクト）として注入できるようにし、テストでは偽物に差し替える。
- コマンドがない（`FileNotFoundError`）、タイムアウト、終了コード ≠ 0、JSON が壊れている → そのソースは「読めなかった」（`SourceError`）。

### 5.2 Herdr

1. `herdr api snapshot` を1回呼ぶ。応答は `{"result": {"snapshot": {...}}}`。使うキー:
   - `workspaces[]`: `workspace_id`, `label`, `number`
   - `tabs[]`: `tab_id`, `label`, `number`
   - `panes[]`: `pane_id`, `workspace_id`, `tab_id`, `label`, `title`, `terminal_title_stripped`, `agent`, `display_agent`, `agent_status`, `cwd`, `foreground_cwd`, `focused`
2. 各ペインについて `herdr pane process-info --pane <pane_id>` を呼び、`result.process_info.foreground_processes[0].cmdline` を COMMAND にする。
   - 失敗したペインは COMMAND を `null`（表では `-`）にするだけで、ソース全体は失敗にしない。
   - ペイン数ぶん呼ぶので、`concurrent.futures.ThreadPoolExecutor(max_workers=8)` で並列に呼ぶ。
3. 対応関係:
   - SESSION = ワークスペースの `label`（なければ `workspace_id`）
   - PANE = ペインの `label` → `title` → タブの `label` → `pane_id` の順で、最初に空でないもの。
     同じワークスペースに同じ PANE 名が複数あるときは、2つ目以降に `#<pane_id>` を付けて区別する。
   - AGENT = `display_agent` → `agent` → なし
   - STATUS = `agent_status`（§6）
   - CWD = `foreground_cwd` → `cwd`
   - スナップショットにはないキーがあっても落ちないこと（すべて `.get()` で読む。必須は `pane_id` と `workspace_id` のみ）。

### 5.3 tmux

1. 次を1回呼ぶ（区切りはタブではなく、ペイン名に含まれにくい `\x1f`（Unit Separator））:

   ```
   tmux list-panes -a -F '#{session_name}\x1f#{window_index}\x1f#{pane_index}\x1f#{pane_id}\x1f#{pane_title}\x1f#{pane_current_command}\x1f#{pane_pid}\x1f#{pane_dead}\x1f#{pane_active}\x1f#{window_active}\x1f#{session_attached}\x1f#{pane_current_path}\x1f#{pid}:#{start_time}:#{pane_pid}'
   ```

   （`-F` に渡すときは Python 側で実際の `\x1f` 文字を入れる。tmux はそのまま出力する）
2. 終了コード ≠ 0 で stderr に `no server running` または `error connecting` を含む → 0 件（エラーではない）。
3. 対応関係:
   - SESSION = `session_name`
   - PANE = `<window_index>.<pane_index>`。`pane_title` が空でもホスト名でもなければ `<window_index>.<pane_index> <pane_title>`
   - COMMAND = `pane_pid` の子プロセスのうち最後のものの `/proc/<pid>/cmdline`（NUL 区切りを空白で連結）。読めなければ `pane_current_command`。
     （`/proc` を読む処理も差し替え可能にしてテストする）
   - AGENT = COMMAND の先頭の実行ファイル名（basename）か、その引数に含まれるスクリプト名（`node .../claude` のような場合）が、既知のエージェント名 `claude`, `codex`, `gemini`, `agy`, `opencode`, `aider`, `cursor-agent`, `copilot`, `kimi`, `pi` のいずれかならその名前。なければなし。
   - STATUS = §6
   - CWD = `pane_current_path`
   - focused = `pane_active == 1 and window_active == 1 and session_attached > 0`

## 6. 状態（STATUS）

| 値 | herdr | tmux | orca（優先順位は §11.5） |
|---|---|---|---|
| `working` | `agent_status == "working"` | — | ホームに working |
| `blocked` | `agent_status == "blocked"` | — | ホームに blocked / waiting / permission |
| `done` | `agent_status == "done"` | — | — |
| `idle` | `agent_status == "idle"` | — | ホームに idle / done |
| `running` | — | AGENT がある、または COMMAND がシェル以外 | — |
| `shell` | — | COMMAND がシェル（`bash`, `zsh`, `fish`, `sh`, `dash`, `nu`）だけ | — |
| `dead` | — | `pane_dead == 1` | — |
| `unknown` | 上記以外・値なし | — | ホームが不明、または状態が未分類 |
| `paused` | — | — | ボットが一時停止中 |
| `stopped` | — | — | ホームの agents が空 |

tmux からはエージェントの内部状態（作業中か待機中か）が取れないので、v1 では「動いているか」だけを表す。

## 7. モジュール構成

```
src/agent_sessions/
  __init__.py      # __version__ = "0.1.0"
  __main__.py      # python -m agent_sessions
  cli.py           # 引数の解析、ソースの実行、終了コード
  model.py         # Session dataclass、SourceError
  runner.py        # Runner（subprocess 実行）と既定の実装
  herdr.py         # collect(runner) -> list[Session]
  tmux.py          # collect(runner, proc_reader) -> list[Session]
  render.py        # render_table(sessions, width, header) / render_json(sessions)
  textwidth.py     # 全角幅を考慮した幅計算と切り詰め
tests/
  fixtures/        # 実機から取った herdr の応答（herdr_snapshot.json など）
  test_herdr.py    # フィクスチャと偽 Runner で、対応関係・ペイン名の重複・process-info 失敗
  test_tmux.py     # 偽の list-panes 出力で、no server → 0 件、dead、シェル判定、AGENT 判定
  test_render.py   # 列幅、切り詰め（全角を含む）、0 件、JSON
  test_cli.py      # 終了コード（一部失敗 0、全失敗 3）、--source、--agents-only
```

## 8. 完了条件（v1）

1. `python3 -m unittest discover -s tests` が全件成功
2. `./agent-sessions` を実機で実行し、今動いている herdr のペインが表で出る（tmux サーバーが動いていなくても警告なしで動く）
3. `./agent-sessions --format json | python3 -m json.tool` が通る
4. tmux で `tmux new -d -s demo` を作ったあと、`./agent-sessions --source tmux` に `demo` が出る（確認後 `tmux kill-session -t demo`）
5. README.md に使い方と出力例

## 10. send コマンド（v2）

### 10.1 コマンドの形

```
agent-sessions [list] [一覧のオプション…]               # 従来どおり（list は省略可）
agent-sessions send [--no-enter] [--dry-run] [--format {text,json}] <対象> [--] <メッセージ>...
```

- 第1引数が `send` のときだけ send として解釈し、それ以外（`list` か省略）はこれまでの一覧として扱う。既存の呼び出し方（`agent-sessions --source tmux` など）を壊さないこと。
- `<メッセージ>` は複数の引数を空白1つでつないだ文字列。`-` で始まるメッセージは `--` の後ろに書く。
- 既定では、テキストを送ったあとに Enter を1回送る。`--no-enter` で Enter を送らない。
- `--dry-run`：対象の解決と表示だけを行い、実際には送らない。

### 10.2 対象の指定

一覧の表に出ている値をそのまま使えるようにする。

| 書き方 | 意味 |
|---|---|
| `herdr:<SESSION>/<PANE>` | Herdr のワークスペースのラベルと、一覧の PANE 列の値 |
| `herdr:<pane_id>` | Herdr のペイン ID（例：`wN:pH`） |
| `tmux:<SESSION>` | tmux のセッションの、アクティブなペイン（`pane_active` かつ `window_active`） |
| `tmux:<SESSION>:<窓>.<ペイン>` | tmux の特定のペイン（一覧の PANE 列の先頭 `1.2` と同じ番号） |
| `tmux:%<n>` | tmux のペイン ID |
| 接頭辞なし（`<SESSION>/<PANE>` や `<SESSION>`） | 両方のソースから探す。herdr の `<SESSION>/<PANE>`、tmux の `<SESSION>`、`<SESSION>:<窓>.<ペイン>` のどれかに一致するもの |

- 解決には一覧と同じ収集処理（`herdr.collect` / `tmux.collect`）を使う。つまり「送る前に存在を確認する」は、収集結果から対象を見つけることで行う。
- 比較は完全一致（大文字と小文字を区別）。PANE 名に `#<pane_id>` が付いた重複名もそのまま指定できる。
- セッション名やペイン名に `/` や `:` が含まれる場合に備え、接頭辞付きの書き方では、herdr は最後の `/` で、tmux は最後の `:` で分割する。それでも一致しなければ、分割せずに全体をセッション名として探す。
- 一致が0件：エラー（終了コード 4）。stderr に `agent-sessions: send: target not found: <対象>` と、近い候補（セッション名が一致するペイン、最大5件）を出す。
- 一致が2件以上：エラー（終了コード 5）。stderr に候補をすべて `herdr:<SESSION>/<PANE>` か `tmux:<SESSION>:<窓>.<ペイン>` の形で出す。
- 対象のソースが読めない場合（herdr サーバー停止など）：一覧と同じ警告を出し、ほかのソースで探す。すべて読めなければ終了コード 3。
- tmux の状態が `dead` のペインは、送り先として選べない（終了コード 4、理由に dead と書く）。

### 10.3 メッセージの検証

- 空のメッセージ（空白だけを含む）はエラー（終了コード 2）。
- 制御文字（タブ、改行、ESC を含む）を含むメッセージはエラー（終了コード 2）。端末へのエスケープシーケンスの注入、途中の改行による意図しない送信、タブがシェルの補完キーとして働くことを防ぐため。
- 長さの上限は 4096 文字（超えたら終了コード 2）。

### 10.4 送り方

外部コマンドはシェルを使わず、一覧と同じ `Runner` で実行する（テストで差し替えられること）。

- Herdr：
  1. `herdr pane send-text <pane_id> <メッセージ>`（メッセージが `-` で始まる場合でも誤解釈されない形にする。実装時に herdr の引数解析で確かめ、必要なら `--` を挟む）
  2. Enter を送る場合：`herdr pane send-keys <pane_id> enter`（キー名は実装時に herdr で確かめる）
- tmux：各配送で UUID 名の一時バッファに本文を格納し、同じ tmux 呼び出し内で `if-shell -F -t <pane_id>` を使う。サーバー PID・起動時刻・ペインのプロセス PID が収集時と一致し、生存中で入力が有効なら `paste-buffer -d -b <一時名> -t <pane_id>` する。照合できなければ終了6。一時バッファの削除はその配送で生成した名前だけを対象とする。
- Enter は別の UUID と同じ照合条件で改行を貼り付ける（CR に変換される）。本文配送の成功応答を得た後で Enter の配送を確認できなかった場合は、その段階を明示する。タイムアウトなどで応答を得られなかった配送の受信結果は不明とする。
- 対象は必ず ID（herdr の pane_id、tmux の `%n`）で指定する。名前で指定すると、解決から送信までの間に別のペインに当たるおそれがあるため。
- 送信コマンドが失敗したら終了コード 6。stderr に `agent-sessions: send: <理由>`（§3 と同じ JSON の error.message の扱い）。テキストは送れて Enter だけ失敗した場合も 6 とし、そのことを理由に書く。

### 10.5 送ったあとの表示

既定（`--format text`）は stdout に2行。

```
Sent to herdr wsh-2027-pre/1 (wN:pH) [agy, idle]
> 進捗を教えてください ⏎
```

- 1行目：ソース、`<SESSION>/<PANE>`（tmux は `<SESSION>:<窓>.<ペイン>`）、ID、AGENT と STATUS（AGENT がなければ STATUS だけ）。
- 2行目：`> ` に続けて送った内容。Enter を送った場合は末尾に ` ⏎` を付ける。
- `--dry-run` のときは1行目を `Would send to …` にする。
- `--format json`：`{"sent": true|false, "dry_run": bool, "enter": bool, "target": {一覧の JSON と同じ形のオブジェクト}, "message": "…"}`。

### 10.6 終了コード（send）

| コード | 意味 |
|---|---|
| 0 | 送った（`--dry-run` なら、解決できた） |
| 2 | 引数の誤り、メッセージの検証エラー |
| 3 | すべてのソースが読めない |
| 4 | 対象が見つからない（dead を含む） |
| 5 | 対象があいまい（2件以上に一致） |
| 6 | 送信コマンドが失敗した |

### 10.7 モジュール構成の追加

- `target.py`：対象の文字列の解析と、収集結果からの解決（純粋な関数。テストしやすくする）
- `send.py`：メッセージの検証、送信コマンドの組み立てと実行、結果の表示
- `cli.py`：`send` の振り分け
- テスト：`test_target.py`（書き方ごとの解決、0件、複数件、`/` や `:` を含む名前、dead）、`test_send.py`（検証、組み立てられるコマンド、Enter の有無、失敗時の終了コード、表示）、`test_cli.py` に既存の一覧の呼び出し方が壊れていないことの確認を追加

### 10.8 完了条件（v2）

1. `python3 -m unittest discover -s tests` が全件成功（既存の28件を含む）
2. 隔離した tmux サーバー（`TMUX_TMPDIR` を一時ディレクトリにする）で、`send tmux:<セッション> 'echo hello'` を送り、`tmux capture-pane -p` で `hello` が実行されたことを確かめる。`--no-enter` と、存在しないセッション（終了コード 4）も確かめる
3. 実機の herdr では **`--dry-run` だけ**を使い、一覧に出ているペインが解決できることを確かめる。実在のエージェントのペインには送らない。herdr への実際の送信は、偽の Runner によるテストで確かめる（使い捨ての herdr のペインを安全に用意できる場合だけ、そこで実際に送ってもよい）
4. README に send の使い方と出力例を追加

## 11. Orca ソース（v3）

### 11.1 概要

Orca のボットを、一覧のもう1つのソース `orca` として表示する。

- `--source` の選択肢に `orca` を追加する：`{all,herdr,tmux,orca}`。`all` は herdr → tmux → orca の順で3つとも読む。
- `send` の対象には含めない（v3 の範囲外）。`send` の収集は、これまでどおり herdr と tmux だけ。`orca:` の接頭辞は、`send` では「対象が見つからない」（終了コード 4）として扱う。

### 11.2 Orca CLI の見つけ方

次の順で、最初に見つかったものを使う。

1. 環境変数 `AGENT_SESSIONS_ORCA`（空でなければ、そのパス）
2. 環境変数 `ORCA_CLI_COMMAND`（Orca が自分のボットに渡すもの）
3. PATH 上の `orca`
4. PATH 上の `orca.AppImage`

環境変数の明示指定はそのまま使う。自動検出では名前ごとに PATH を先頭から調べ、実行可能な通常ファイルのうち次の配置と同一ファイルの候補だけを使う。

- 登録済み IDE shim は `$XDG_CONFIG_HOME/orca/linux-orca-cli-shim/orca`（設定ディレクトリの既定は `~/.config`）。
- 確認済みの IDE AppImage 配置は `~/.local/opt/orca/orca.AppImage`。

シンボリックリンクも照合し、同名の別プログラムは起動せず次の候補に進む。配置で識別できない単体 AppImage や独自ラッパーは `AGENT_SESSIONS_ORCA` にパスを指定する。

どれも見つからなければ、Orca は 0 件（警告なし）。

> v4（§12.2）で変更：自動検出では、PATH になくても既知の配置を直接使う。

### 11.3 読み方

外部コマンドは、これまでどおり `Runner` で実行する。ただし Orca の CLI は起動に2〜3秒かかるので、Orca の呼び出しだけタイムアウトを 15 秒にする（`Runner` に timeout を渡せるようにするか、Orca 用の既定値を持たせる。テストで差し替えられること）。

1. `<orca> bot list --json`
   - 応答：`{"ok": true, "result": {"bots": [...], "unreachable": [...]}}`
   - 各ボットで使うキー：`id`, `name`, `agent`, `paused`, `specialty`
   - `unreachable`（接続できない別のホスト）は無視する
2. `<orca> worktree ps --json`（状態を知るため）
   - 応答：`{"result": {"worktrees": [...]}}`
   - `botHome == true` かつ `bot == <ボット名>` の行を、そのボットのホームとする。使うキー：`path`、`agents[].state`
   - この呼び出しが失敗しても、ボットの一覧は出す（状態は §11.5 の `unknown`。ただし `paused` は分かる）

### 11.4 列の対応

| 列 | Orca での値 |
|---|---|
| SOURCE | `orca` |
| SESSION | ボット名（`name`） |
| PANE | `home`（ボットのホームのチャット。v3 ではボット1つにつき1行） |
| AGENT | `agent`（`claude` / `codex` / `antigravity` など、値をそのまま） |
| STATUS | §11.5 |
| COMMAND | 専門分野（`specialty`）。未設定なら `-` |
| CWD | ボットのホームの `path`（分からなければ `-`） |

- JSON では、`pane_id` にボットの `id` を入れ、`focused` は `false`。
- JSON のすべての行に `specialty` を追加する（herdr と tmux では `null`）。orca の行では、`command` にも同じ値を入れる（表との対応を保つため）。
- 並び順：ボット名の順（Python の文字列比較）。
- `--agents-only`：Orca のボットは常にエージェントなので、すべて含める。
- 表の COMMAND 列の見出しは変えない（README に「Orca の行では専門分野を表示する」と書く）。

### 11.5 状態（STATUS）

| 条件（上から順に判定） | STATUS |
|---|---|
| `paused == true` | `paused` |
| worktree ps が読めない、またはホームの行がない | `unknown` |
| ホームの `agents[].state` に `working` がある | `working` |
| `blocked`、`waiting`、`permission` のどれかがある | `blocked` |
| `idle` か `done` がある | `idle` |
| `agents` が空 | `stopped` |
| それ以外 | `unknown` |

§6 の表に `paused` と `stopped` を加える。

### 11.6 0件として扱う場合（エラーにしない）

次の場合は Orca の行を 0 件とし、警告も出さず、終了コードにも影響させない（tmux サーバーがないときと同じ）。

- Orca CLI が見つからない、起動に失敗する、タイムアウトする
- 終了コードが 0 以外（Orca が起動していない場合を含む）
- 応答が JSON でない、`ok` が `true` でない、`result.bots` が配列でない

`--source orca` だけを指定して 0 件の場合も、終了コードは 0（「読めた」とみなす）。

### 11.7 モジュール構成の追加

- `orca.py`：`collect(runner) -> list[Session]`、CLI の見つけ方（環境変数と PATH の探索を差し替えられるようにする）
- `model.py`：`Session` に `specialty: str | None = None` を追加（JSON の項目が1つ増える）
- `cli.py`：`--source` に `orca` を追加
- テスト：`test_orca.py`（実機の応答から作ったフィクスチャ、paused・working・blocked・idle・stopped・unknown の判定、worktree ps の失敗、CLI がない・非ゼロ・壊れた JSON・`ok: false` で 0 件、見つけ方の優先順位）。`test_cli.py` に、`--source all` で3つのソースが順に並ぶこと、`--source orca` で 0 件でも終了コード 0 になること、`send` が orca の行を対象にしないことを追加

### 11.8 完了条件（v3）

1. `python3 -m unittest discover -s tests` が全件成功（既存の66件を含む）
2. 実機で `./agent-sessions --source orca` に、いまある Orca のボット（claude、codex、gemini）が、エージェントの種類・状態・専門分野つきで出る
3. `./agent-sessions` で herdr → tmux → orca の順に並び、`--format json` で orca の行が他と同じ形（＋ `specialty`）で出る
4. `AGENT_SESSIONS_ORCA=/nonexistent ORCA_CLI_COMMAND= PATH=/usr/bin:/bin ./agent-sessions --source orca` が、警告なし・0件・終了コード 0 になる
5. Orca の CLI は読むだけにする（`bot list` と `worktree ps` 以外は呼ばない）
6. README に Orca ソースの説明を追加

## 12. E2E テストと Orca 検出の修正（v4）

### 12.1 背景

v3 の自動検出は、既知の配置（`~/.local/opt/orca/orca.AppImage` など）に Orca があっても、それが PATH から見えないと使わなかった。そのため、実機で `--source orca` が 0 件になった。単体テストはすべて偽の Runner とフィクスチャで動いていて、実機の組み合わせを確かめるテストがなかった。設計者（claude）もレビューでこの挙動に気づいていたのに、「仕様どおり」として通してしまった。v4 では実機に対する E2E テストを要件にし、検出を直す。

### 12.2 Orca CLI の見つけ方（§11.2 を置き換え）

次の順で、最初に使えるものを使う。

1. 環境変数 `AGENT_SESSIONS_ORCA`（空でなければ、そのパスをそのまま使う）
2. 環境変数 `ORCA_CLI_COMMAND`（同上）
3. 既知の配置を、**PATH に関係なく**直接調べる（この順）
   1. 登録済みの shim：`${XDG_CONFIG_HOME:-$HOME/.config}/orca/linux-orca-cli-shim/orca`
   2. AppImage：`$HOME/.local/opt/orca/orca.AppImage`
4. どれもなければ 0 件（警告なし）

- PATH の探索はやめる。PATH 上の `orca` / `orca.AppImage` は、既知の配置へのシンボリックリンクでない限り、同名の別プログラム（GNOME のスクリーンリーダーなど）かもしれないので使わない。既知の配置へのリンクであれば、手順3で同じ実体がすでに見つかるので、PATH を見る必要がない。
- 既知の配置のファイルを使う条件（偽物を実行しないため）：
  - 絶対パスで、`stat`（リンクをたどった実体）が通常ファイルで、実行できる
  - 実体の所有者が、実行しているユーザーか root である
  - 実体がグループと他人から書き込めない（`mode & 0o022 == 0`）
  - 条件を満たさなければ、その候補は飛ばして次へ進む（警告なし）
- 呼び出すときは、見つけたパスをそのまま使う。
- shim を AppImage より先に試す理由：shim は動いている Orca を直接呼ぶので、速い（約0.2秒。AppImage を直接呼ぶと約3.6秒）。shim が「Orca CLI is unavailable」で失敗したら（Orca を開き直したあとなど）、AppImage で1回だけやり直す。

### 12.3 E2E テスト

#### 置き場所と実行方法

- テストは `tests_e2e/` に置く。`python3 -m unittest discover -s tests`（単体テスト）には含めない。
- 実行方法：`./run-e2e-tests.sh`（中身は `PYTHONPATH=src python3 -m unittest discover -s tests_e2e -v`）。
- 環境が足りない場面（Herdr が起動していないなど）は、その場面のテストを **skip** にして理由を表示する。`E2E_REQUIRE_ALL=1` を付けると、skip を失敗として扱う（実機ですべての場面を必ず確かめたいとき用）。
- すべてのテストは、本物の `./agent-sessions` を `subprocess` で実行する（関数を直接呼ばない）。出力は基本的に `--format json` で受けて検証する。
- 全体で 2 分以内に終わること。

#### 安全上の決まり

- 実在の Herdr のペインや、実在のエージェントには **何も送らない**。Herdr に対しては、一覧と `send --dry-run` だけ。
- Orca の CLI に対しては、読むだけ（`bot list --json` と `worktree ps --json`）。
- tmux は、テストが `TMUX_TMPDIR` に一時ディレクトリを指定して立てる隔離サーバーだけを使う。`TMUX` と `TMUX_PANE` は環境から外す。テストの最後に必ず `kill-server` し、一時ディレクトリを消す。
- 正解の値（期待値）は、テストが本物のツールから直接取る（`herdr api snapshot`、`tmux list-sessions`、`~/.local/opt/orca/orca.AppImage bot list --json`）。フィクスチャは使わない。

#### 必ず含める場面

| # | 場面 | 前提（足りなければ skip） | 確かめること |
|---|---|---|---|
| E1 | Herdr が起動していて、実際のペインがある | `herdr api snapshot` が成功し、ペインが1つ以上ある | `--source herdr --format json` のすべての `pane_id` が、snapshot のペイン ID の集合と一致する。SESSION がワークスペースのラベルと一致する |
| E2 | tmux が起動していて、実際のセッションがある | `tmux` コマンドがある | 隔離サーバーにセッション（例：`e2e-日本語`、ペイン2つ）を作り、`--source tmux --format json` にそのセッションと2つのペインが出る。終了コード 0 |
| E3 | Orca が既知の場所にあるが、PATH にない | `~/.local/opt/orca/orca.AppImage` が実行でき、それで直接 `bot list --json` が成功する | `env -u ORCA_CLI_COMMAND -u AGENT_SESSIONS_ORCA PATH=/usr/bin:/bin ./agent-sessions --source orca --format json` で、ボット名の集合が AppImage の `bot list` の結果と一致する（0 件ではない） |
| E3b | AppImage だけがあり（shim がない）、PATH にもない | E3 と同じ | `XDG_CONFIG_HOME` を空の一時ディレクトリにして shim を見えなくし、PATH を制限して `--source orca` を実行する。ボット名の集合が一致する（報告された不具合そのものの場面） |
| E3c | shim が古くなっている（Orca を開き直した後など） | E3 と同じ | 一時的な `XDG_CONFIG_HOME` に「Orca CLI is unavailable」を出して失敗する shim を置く。shim が先に試され、AppImage でやり直してボットが出る |
| E4 | Orca が起動していて、実際のボットがいる | E3 と同じ | `--source orca --format json`（通常の環境）で、ボット名・`agent`・`paused` の対応が `bot list` と一致する。各行の `status` が §11.5 の値のどれか |
| E5 | `--source all` で3つを同時に表示 | E1〜E4 の前提がすべてある | 隔離 tmux を立てた状態で `--format json` を実行し、`source` に `herdr`・`tmux`・`orca` の3つが、この順で現れる。表形式（`--width 200`）でも3つが出る |
| E6 | `send --dry-run` で実際のペインを解決 | E1 と E2 の前提 | Herdr：snapshot の最初のペインを `herdr:<SESSION>/<PANE>` と `herdr:<pane_id>` で指定し、`Would send to` とその ID が出て終了コード 0（何も送られていないことは、`--dry-run` であることで保証）。tmux：隔離サーバーで `tmux:<セッション>` を解決できる |
| E7 | 偽の orca を実行しない | なし | 一時ディレクトリに、呼ばれたら印を残す偽の `orca` を作り、PATH の先頭に置いて `--source orca` を実行する。印が残らないこと |
| E8 | tmux への実際の送信（隔離サーバー） | `tmux` コマンドがある | 隔離サーバーの `cat` ペインに `send` で送り、`capture-pane` で本文が届いたことを確かめる |

### 12.4 完了条件（v4）

1. `python3 -m unittest discover -s tests` が全件成功（既存の82件を含む。検出の変更に合わせて単体テストも直す）
2. `./run-e2e-tests.sh` がこの機械で E1〜E8 すべて成功（skip なし。`E2E_REQUIRE_ALL=1 ./run-e2e-tests.sh` で確かめる）
3. 実機で `env -u ORCA_CLI_COMMAND PATH=/usr/bin:/bin ./agent-sessions --source orca` にボットが出る
4. README に E2E テストの実行方法・前提・安全上の決まりを書く

### 12.5 レビューの決まり（v4 から）

レビュワーは、単体テストに加えて `E2E_REQUIRE_ALL=1 ./run-e2e-tests.sh` を必ず実行し、その結果を報告に書く。

### 12.6 報告の証跡（v4 から、すべての報告に適用）

報告では主張だけでなく、実行したコマンドと、その出力をそのまま載せる。

- 一覧表示：実際の `./agent-sessions` の出力
- テスト：実行したコマンドと、件数・合格数が分かる出力（`Ran N tests` と `OK` / `FAILED` の行、E2E は各テストの名前と結果の行）
- 修正の確認：修正前と修正後の、同じコマンドの出力を両方

## 13. macOS 対応と英語版 README（v5）

### 13.1 範囲

- Orca CLI の自動検出に macOS の配置を加える。
- tmux のペインのプロセス情報を、macOS では `ps` で読む。
- README.md を英語版に置き換え、第三者レビュー（Carbon）の指摘 13 点を反映する。
- Linux の動作は変えない（既存の単体テストと E2E がそのまま通ること）。

### 13.2 プラットフォームの選び方

- `platform.system()` の値で選ぶ。`"Darwin"` なら macOS、`"Linux"` なら Linux。それ以外（Windows など）は、Orca の自動検出は 0 件、tmux のプロセス情報は tmux のコマンド名だけ（§13.4 の最後の手段）。
- テストで差し替えられるように、`platform.system` を引数か関数で注入できるようにする。

### 13.3 Orca CLI の見つけ方（§12.2 に macOS を追加）

1. `AGENT_SESSIONS_ORCA`、2. `ORCA_CLI_COMMAND`（どちらも、これまでどおり最優先でそのまま使う）
3. 既知の配置を直接調べる（PATH は使わない）

| プラットフォーム | 順番 | パス | 種類 |
|---|---|---|---|
| Linux | 1 | `${XDG_CONFIG_HOME:-~/.config}/orca/linux-orca-cli-shim/orca` | shim |
| Linux | 2 | `~/.local/opt/orca/orca.AppImage` | 本体 |
| macOS | 1 | `~/.local/bin/orca` | shim（設定 → General → Orca CLI で登録したもの） |
| macOS | 2 | `/Applications/Orca Desktop.app/Contents/MacOS/orca` | 本体 |
| macOS | 3 | `~/Applications/Orca Desktop.app/Contents/MacOS/orca` | 本体 |

- 出典：Orca 公式ドキュメント（https://orca-desktop.com/docs/cli/）。
- 偽物を実行しない条件（実体が通常ファイルで実行でき、所有者が自分か root、`mode & 0o022 == 0`）は、どのパスにも同じように適用する。
- `~/.local/bin/orca` は macOS のときだけ候補にする（Linux では、同じ場所に別のプログラムがあり得るため）。
- shim が「Orca CLI is unavailable」で失敗したときに本体で1回だけやり直す動作（§12.2）は、macOS でも同じ。やり直し先は、表の順で最初に条件を満たす本体。

### 13.4 tmux のプロセス情報（§5.3 に macOS を追加）

Linux はこれまでどおり `/proc/<pid>/task/<pid>/children` と `/proc/<pid>/cmdline` を使う。

macOS では、収集1回につき次を1回だけ呼ぶ（ペインごとに呼ばない）。

```
ps -A -ww -o pid=,ppid=,args=
```

- `-ww` で長いコマンドラインが切られないようにする。
- 出力から「親 PID → 子 PID の一覧」と「PID → args」の対応表を作る。
- ペインの `pane_pid` の子のうち、PID が最大のもの（いちばん新しいもの）の args を使う。子がなければ `pane_pid` 自身の args を使う。どちらもなければ、tmux の `pane_current_command` を使う（最後の手段。Linux と同じ）。
- `ps` が使えない、失敗する、出力が読めないときは、すべてのペインで最後の手段を使う（tmux ソース全体は失敗にしない）。
- macOS の `ps` の args は、引数の区切りが空白でつながった1つの文字列で返る。AGENT の判定（§5.3）のために空白で分けて argv とみなす。そのため、空白を含むパスを引数に持つコマンドでは、Linux より判定が粗くなる（既知の制限として README に書く）。
- `ps` の呼び出しも `Runner` を通す（テストで偽の出力を与えられるようにする）。

### 13.5 テスト

単体テスト（どの OS でも実行できること）
- Orca：プラットフォームを `Darwin` に差し替え、一時ディレクトリを HOME にして、表の各パスの優先順位、条件を満たさない候補を飛ばすこと、Linux では `~/.local/bin/orca` を見ないこと、`Windows` では 0 件になることを確かめる。`/Applications` は実際のパスを使わず、検出関数に「ルート」を渡せるようにして一時ディレクトリで再現する。
- tmux：プラットフォームを `Darwin` に差し替え、偽の `ps` 出力で、子の選び方（PID が最大のもの）、子がない場合、`ps` の失敗、空白で区切る argv、AGENT の判定を確かめる。

E2E
- 既存の E1〜E8 は、Linux ではそのまま通ること。
- E2 を強くする：隔離 tmux のペインで `sleep 300` のような子プロセスを動かし、COMMAND にその引数まで出ることを確かめる（Linux では /proc、macOS では ps の経路を、実機で通す）。
- E9（macOS のみ）：Orca の本体を既知の配置で検出して一覧が出る。macOS でなければ「not macOS」の理由で skip する。`E2E_REQUIRE_ALL=1` でも、プラットフォームが違うことによる skip は失敗にしない（その場面は、その OS でしか成り立たないため）。
- Linux 用の E3b と E3c は、macOS では skip する（同じく、失敗にはしない）。

既知の制限（報告と README に書く）
- macOS の実機検証はできていない。依頼に書かれた Mac mini（100.84.227.27）には Orca が入っていない。また、この作業環境からは SSH の公開鍵で入れなかった（`Permission denied (publickey,password,keyboard-interactive)`）。macOS の経路は、単体テストでの再現だけで確かめる。

### 13.6 README（英語版に置き換え）

README.md は英語で書き直す。日本語版は残さない。次の 13 点を必ず反映する。

1. `--source all` で Orca が読めないとき、Orca は「読めたソース」に数えない。Orca が読めないのに終了コード 0 になるのは `--source orca` 単独のときだけ。`all` で Herdr と tmux も読めなければ、終了コードは 3
2. `E2E_REQUIRE_ALL=1` の Orca の前提：Orca が起動しているだけでなく、既知の配置に本体があり、ボットを1つ以上取得できること
3. 「finished pane」ではなく「dead pane」（コードの状態名）
4. 接頭辞なしの対象は、名前による指定だけ（`wN:pH` や `%12` のような ID は不可）。複数に一致したら、候補を stderr に出す
5. tmux の `running` は「シェル以外の前面プロセスが動いている」（`shell`、`dead` と対比する）
6. E2E の行から「(10 scenarios)」のような件数を外す
7. Orca CLI の検出パス（Linux と macOS）と、環境変数（`AGENT_SESSIONS_ORCA` / `ORCA_CLI_COMMAND`）を書く。PATH は探さず、既知のパスだけを調べることも書く
8. 冒頭の文："List Herdr and tmux panes alongside Orca bots—showing agent, status, foreground command, and working directory—and send messages to panes."
9. オプション表：`--source` は "Sources to read (`all` queries Herdr, tmux, then Orca)."、`--width` は "Table width (untruncated when piped)."
10. send の節では "body" ではなく "message" と書く
11. テストの節に、安全上の注意（実機の E2E は、既存の Herdr のペインや Orca のボットに入力を送らないこと。送信は、一時的に立てた隔離 tmux サーバーにだけ行うこと）を書く。設計書が日本語であることも書く
12. 例に出てくる実在のワークスペース名やパス（`wsh-2027-pre`、`~/Work/laiso/...` など）を、汎用のもの（`my-app`、`~/work/my-app`）に置き換える。出力例も同様
13. 対応プラットフォーム：Linux が主。macOS にも対応する（Orca はアプリ本体か登録済みの shim を自動検出、tmux のプロセス情報は `ps`）。§13.4 と §13.5 の macOS の制限も書く

### 13.7 完了条件（v5）

1. `python3 -m unittest discover -s tests` が全件成功（既存の86件を含む）
2. `E2E_REQUIRE_ALL=1 ./run-e2e-tests.sh` が Linux で成功（macOS 専用の場面は「not macOS」で skip になり、失敗にはならない）
3. 修正の前と後で、Linux の `./agent-sessions --source all` と `--source orca` の出力が同じこと（Linux の動作を変えていない証跡）
4. README.md が英語版になり、§13.6 の13点がすべて反映されている（レビュワーが1点ずつ確かめる）
5. 報告には、§12.6 の証跡（コマンドとその出力）を必ず載せる

## 9. 設計へのフィードバック

実装中に設計と食い違う点・設計が足りない点が見つかったら、このファイルの末尾に「## 変更履歴」を作って追記し、報告に書くこと。

## 変更履歴

- v1 実装時に §4.1 の境界条件を明確化した。0件の表は見出しの指定にかかわらず `No sessions found.` の1行。幅8は COMMAND と CWD を縮める際の下限で、元から短い列は広げない。指定幅に収まらない場合は下限と固定列を優先する。表のセルに含まれる改行・制御文字は空白で表示し、JSON には元の値を保持する。
- §4.1 の Herdr の整列はワークスペース番号、タブ番号を優先する。同じタブ内は表示名、同名ならペイン ID 順。番号がない項目は番号付き項目の後ろに表示する。
- §5.3 の「最後の子」は Linux の `/proc/<pane_pid>/task/<pane_pid>/children` の末尾の PID とする。引数に空白を含むパスも扱えるよう、AGENT 判定には空白連結前の引数を使う。
- （claude レビュー 1 回目）§5.3 PANE：bash などが自動で付ける `user@host:cwd` 形式のタイトルも、ホスト名と同じく既定のタイトルとして表示しない（実機で PANE 列が長くなり、表が読みにくくなっていたため）。
- （claude レビュー 1 回目）§5.3 COMMAND：子プロセスがないペイン（`tmux new 'sleep 300'` のように直接起動したもの）は、ペインのプロセス自身の `/proc/<pane_pid>/cmdline` を使う。`pane_current_command` は最後の手段とする。
- （claude レビュー 1 回目）§5.3 AGENT：引数のどこかにエージェント名があるだけでは判定しない（`vim claude` を誤判定していた）。実行ファイル名か、インタプリタ（node, npx, python など）が最初に受け取るスクリプト名だけを見る。
- （claude レビュー 1 回目）§3 警告：外部コマンドが JSON の `{"error":{"message":...}}` を返したときは、その message だけを警告に出す（herdr サーバーが止まっているときに、JSON がそのまま出ていたため）。
- （codex レビュー 1 回目への対応）§5.3 AGENT：インタプリタのオプションのうち引数を取るもの（node の `--require` など、python の `-W`、npx の `-p` など）は、その引数を飛ばしてからスクリプト名を判定する。`python -m <モジュール>` はモジュール名で判定する。`-e`、`-c` のようにコードを直接渡す場合は判定しない。
- （codex レビュー 1 回目への対応）§5.3 PANE：空白で区切られたタイトルは、後ろが `~` か `/` で始まるパスのときだけ既定のタイトルとみなす（`user@host deploy review` のような明示の名前は表示する）。
- （codex レビュー 1 回目への対応）§3 警告：stdout と stderr の両方から JSON の error.message を探し、どちらにもないときだけ通常のテキストを使う。
- （codex レビュー 2 回目への対応）§5.3 AGENT：コードを直接渡すオプションはランタイムごとに判定する（node・bun の -e と -p、python の -c。deno と bun の -c は設定ファイル）。--opt=value の形と、python の連結形（-maider、-ccode）も解釈する。uv run などの起動用サブコマンドは1回だけ読み飛ばす。npx の --workspace、uv の --with-requirements などを、値を取るオプションに追加した。判定はベストエフォートで、知らないオプションは値を取らないものとみなす。
- （codex レビュー 2 回目への対応）§5.3 PANE：空白区切りの既定タイトルで、パスに空白が含まれていても既定のタイトルとみなす。
- v2 §10.4。実機の `herdr --version` は 0.9.5。`herdr pane send-text --help` と `send-keys --help` で位置引数の形を確認し、同版の公式ソースで送信せずに解析経路を確認した。`send-text` は ID より後の全引数を空白連結するので、`--` を追加するとその文字も本文に入る。通常の `-abc`、`--help`、本文そのものの `--` は1引数でよい。ただし `--session`、`--remote`、`--remote-keybindings`（各 `=値` 形式を含む）はサブコマンドより先に解析される。その本文に限って先頭の `-` と残りを同じ ID に順に send-text し、2回目が失敗したら先頭だけ送信済みと報告する。[位置引数の処理](https://github.com/herdrdev/herdr/blob/v0.9.5/src/cli/pane.rs#L1017-L1039)・[ヘルプの判定](https://github.com/herdrdev/herdr/blob/v0.9.5/src/cli/spec.rs#L74-L106)・[セッション引数](https://github.com/herdrdev/herdr/blob/v0.9.5/src/session.rs#L56-L80)・[リモート引数](https://github.com/herdrdev/herdr/blob/v0.9.5/src/remote/args.rs#L43-L112)
- v2 §10.4。Herdr の send-keys はキー文字列を API に渡し、API のキー解析で小文字の `enter` は `KeyCode::Enter` に対応する。公式 v0.9.5 のソースと偽 Runner で確認し、実機のペインでのキー受信は未検証。[API のキー解析](https://github.com/herdrdev/herdr/blob/v0.9.5/src/app/api_helpers.rs#L11-L15)・[キー名の対応](https://github.com/herdrdev/herdr/blob/v0.9.5/src/config/keybinds.rs#L1308-L1316)
- v2 §10.2。tmux のアクティブ判定はクライアント接続の有無にかかわらず保持し、一覧 JSON の `focused` と区別する。Herdr のペイン名自体に `/` がある場合は、最後の `/` での分割に一致する行がなければ、一覧の `SESSION/PANE` を丸ごと照合する。明示的な ID 形式は、対象が消えたときに同名のセッションへ送らないよう ID だけで照合する。
- （claude レビュー v2 の 1 回目）§10.3：タブも受け付けない。tmux と herdr に送ったタブは文字ではなく Tab キーとして届き、シェルの補完が働いて内容が変わるため（隔離した tmux で確認）。
- （claude レビュー v2 の 1 回目）§10.2：エラーのときの候補一覧に見出し（あいまいなときは `candidates:`、見つからないときは `did you mean:`）を付け、字下げして表示する。
- （claude レビュー v2 の 1 回目）検証：一時的な HOME で隔離した herdr サーバー（`HOME=<一時ディレクトリ> herdr server`）を使い、実際の herdr でも send-text と Enter の送信、`--session=x` のような本文の分割送信が正しく動くことを確かめた。
- （codex 相互レビュー v2 の 1 回目への対応）§10.4 tmux：P1。ウィンドウで synchronize-panes が on だと、send-keys がウィンドウ内のすべてのペインに届く。送る前に `#{pane_synchronized}` を調べ、1 なら何も送らずに終了コード 6 で終える（ユーザーの設定は変えない）。
- （codex 相互レビュー v2 の 1 回目への対応）§10.4 tmux：末尾が `;` の引数は、tmux のコマンド区切りとして解釈される。末尾の `;` を `\;` に置き換えて、文字どおり届くようにする（`a;`、`b\;`、`;`、`c;;` を隔離した tmux で確認）。
- （codex 相互レビュー v2 の 1 回目への対応）§10.2：見つからないときの候補からは、指定した対象そのものと dead のペインを除く。選べる候補がなければ、見出しも出さない。
- （codex 相互レビュー v2 の 2 回目への対応）§10.4 tmux：P1 2件（同期を確かめてから送るまでの間に設定が変わる競合と、同期の状態が分からないときにも送ってしまう件）を、方式の変更でまとめて解消した。send-keys をやめ、`set-buffer` と `paste-buffer` を1回の tmux 呼び出しのコマンド列で送る。paste-buffer は synchronize-panes が on でも指定したペインにだけ届き（隔離した tmux 3.8 で、cat を2ペインで動かし同期を on にして確認）、Enter も改行の貼り付け（CR になる）で送る。そのため同期の設定に左右されず、確認の手順もいらない。前回入れた `#{pane_synchronized}` の確認はやめた。失敗したときは一時バッファを消す。
- （codex 相互レビュー v2 の 3 回目への対応）§10.4 tmux：一時バッファの名前を呼び出しごとの UUID にした（PID だと、同じ名前のユーザーのバッファを上書き・削除し得たため）。本文と Enter を別の tmux 呼び出しに分け、失敗の理由を「nothing sent」と「text sent, Enter failed」で区別して、再送による二重入力を防ぐ。後片付けの delete-buffer に失敗したときは、バッファ名と「may remain」を理由に書く（バッファがもうない場合は何も足さない）。
- （codex 修正レビュー）§5.3・§10.4。隔離サーバーの再起動で同じ `%0` が再利用され、本文とは別のペインに Enter が届くことを再現した。収集時のサーバー PID・起動時刻・ペインプロセス PID を内部に保持し、本文・Enter ごとに tmux 内で同一性と入力可否を照合する。入力無効時の成功表示、応答消失時の未送信の断定を修正した。後片付けは当該 UUID のバッファが既に存在しないと確認できたエラーだけを無視する。
- （claude レビュー：codex の 9d1f246 に対して）§10.4 tmux：tmux 自身の確認（if-shell）でペインが拒否された場合は、貼り付けていないことが確実なので「nothing sent」（Enter のときは「text sent, Enter not sent」）と書く。「not confirmed」は、tmux からの応答が失われた場合など、届いたかどうか分からないときだけに使う。
- （codex 修正レビュー）§10.4 tmux：フックの表示が stdout に混在しても誤認しないよう、各配送の UUID を含む確認行を成功・拒否の各分岐から返す。本文の成功行を確認した場合だけ Enter に進む。確認行の欠落・矛盾、非ゼロ終了、タイムアウトでは該当段階の配送は未確認とする。一時バッファの削除にも失敗した場合は、判明済みの配送結果を維持してバッファ名と削除エラーを併記する。 タイムアウトの診断には時間を出し、本文・内部識別情報を含むコマンド引数は展開しない。
- v3 §11.6。Orca 単独では未導入・起動失敗・不正応答を警告なしの0件・終了0とする。`all` ではその Orca を成功ソースに数えず、Herdr と tmux も読めなければ従来どおり終了3。有効な `bots: []` は成功として数える。空配列の場合は状態照会を省く。
- v3 §11.3。ボットの名前と ID が取得できる行を表示する。ホーム行の agents が欠落・不正なら unknown、明示的な空配列なら stopped とする。worktree ps は ok を省略した result 形式も受け付け、ok が明示されて true 以外ならホーム情報を使わない。
- （claude レビュー v3：codex の 57d1f54 に対して）§11.2：PATH 上の `orca` が GNOME のスクリーンリーダー（Arch では `orca` パッケージで /usr/bin/orca に入る、同じ名前の別のプログラム）の場合は使わない。`bot list --json` で起動すると、読み上げが始まるなどの副作用がありえるため。先頭が python の shebang で、`from orca import` などを含むスクリプトは飛ばして、次の候補（`orca.AppImage`）に進む。環境変数で明示された場合は、そのまま使う。
- （codex 相互レビュー v3）§11.2。shebang やソースの表記による除外を、登録済み shim・実機の IDE 用 orca.desktop の Exec で確認した AppImage 配置とのファイル同一性の照合に変更した。名前別・PATH 順に全候補を調べ、リンク経由でも一致した既知配置のパスを起動する。標準外の配置は環境変数で明示する。
- v4 §12.2。自動検出は既知の shim・AppImage を直接調べ、所有者と書込権限はリンク先の stat で確認する。自動 shim の unavailable 診断が bot list または worktree ps に返った場合、失敗した照会だけを次の AppImage で再試行し、その後も同じ AppImage を使う。明示指定の失敗やその他の診断では切り替えない。
- （claude レビュー v4：codex の 1700134 に対して）§12.3：E3 は、この機械では登録済みの shim で成功しており、報告された不具合（AppImage しかなく、PATH にもない）の場面を通っていなかった。そこで E3b（shim なし・AppImage だけ）と E3c（古い shim から AppImage へのやり直し）を追加した。修正前の main c2df10a では、E3、E3b、E3c の3つとも失敗することを確かめた。
- v4 §12.3 の E3b・E3c は、ボット名の一致に加え、CLI の子プロセス起動イベントをテスト時のみ記録する。実行ファイル・引数・順序・回数を照合し、E3b は AppImage の2照会、E3c は一時 shim の bot list に続く AppImage の同じ照会1回と worktree ps 1回を要求する。参照用の直接照会はこの記録の対象外とする。
- （claude レビュー v5：codex の 9ce4c15 に対して）§13.5：macOS の `ps` の経路は、偽の出力による単体テストでしか確かめていなかった。しかし Linux の procps も `ps -A -ww -o pid=,ppid=,args=` を受け付ける。そこで、テスト専用の sitecustomize（tests_e2e/darwin）で platform.system を Darwin に差し替え、本物の ps と隔離 tmux に対して E10 を実行するようにした（Linux の /proc の経路と結果が一致することも確かめる）。README の例にあった実在のペイン ID（wN:pH）を w1:p1 にし、Mac mini という内部の事情を README から外した。
- （codex 相互レビュー v5）§13.5 E10 は CLI の収集ごとに実行ファイル・引数・回数を照合する。Darwin 分岐では ps が1回で /proc のプロセス引数を読まず、Linux の比較用収集では対象ペインの /proc を読み ps を使わないことを確認する。macOS では OS 判定を置き換えず同じ ps の検査を行う。
