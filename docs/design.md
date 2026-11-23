# agent-sessions 設計書 v2（v1 ＋ send コマンド）

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
agent-sessions [--source {all,herdr,tmux}] [--format {table,json}]
               [--agents-only] [--no-header] [--width N]
```

| オプション | 既定 | 意味 |
|---|---|---|
| `--source` | `all` | 読むソース。`all` は herdr と tmux の両方 |
| `--format` | `table` | `table`: 人が読む表、`json`: 機械向けの JSON 配列 |
| `--agents-only` | オフ | エージェントが動いている行だけにする（herdr は `agent` があるペイン、tmux は §5.3 の判定で agent が付いたペイン） |
| `--no-header` | オフ | 表の見出し行を出さない |
| `--width N` | 端末幅 | 表の全体幅。未指定なら `shutil.get_terminal_size().columns`、端末でないとき（パイプ）は切り詰めない |

### 終了コード

- `0`: 指定したソースのうち少なくとも1つが読めた（0件でも 0）
- `2`: 引数の誤り（argparse の既定のまま）
- `3`: 指定したソースがすべて読めなかった（コマンドがない、サーバーが動いていない等）

読めなかったソースは stderr に `agent-sessions: herdr: <理由>` の形で1行警告を出し、残りのソースは表示を続ける。
ただし tmux の「サーバーが動いていない」（`no server running`）は **エラーではなく 0 件**として扱う（警告も出さない）。

## 4. 出力形式

### 4.1 表（既定）

列は次の順。左寄せ、列の間は空白2つ。

| 列 | 内容 |
|---|---|
| `SOURCE` | `herdr` / `tmux` |
| `SESSION` | herdr: ワークスペースのラベル／tmux: セッション名 |
| `PANE` | §5 のペイン名 |
| `AGENT` | エージェント名（なければ `-`） |
| `STATUS` | §6 の状態 |
| `COMMAND` | 前面で動いているコマンドライン |
| `CWD` | 作業ディレクトリ。ホーム配下は `~` に短縮 |

例:

```
SOURCE  SESSION       PANE    AGENT  STATUS   COMMAND                            CWD
herdr   wsh-2027-pre  1       agy    idle     agy --dangerously-skip-permissions ~/Work/laiso/wsh-2027-pre
herdr   wsh-2027-pre  2       -      unknown  /usr/bin/bash                      ~/Work/laiso/wsh-2027-pre
tmux    dev           0.1     claude working  claude                             ~/src/app
```

- 並び順: SOURCE（herdr → tmux）、SESSION、PANE の順（herdr はワークスペース番号・タブ番号の順、tmux はウィンドウ番号・ペイン番号の数値順）。
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
  "focused": false
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
   tmux list-panes -a -F '#{session_name}\x1f#{window_index}\x1f#{pane_index}\x1f#{pane_id}\x1f#{pane_title}\x1f#{pane_current_command}\x1f#{pane_pid}\x1f#{pane_dead}\x1f#{pane_active}\x1f#{window_active}\x1f#{session_attached}\x1f#{pane_current_path}'
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

| 値 | herdr | tmux |
|---|---|---|
| `working` | `agent_status == "working"` | — |
| `blocked` | `agent_status == "blocked"` | — |
| `done` | `agent_status == "done"` | — |
| `idle` | `agent_status == "idle"` | — |
| `running` | — | AGENT がある、または COMMAND がシェル以外 |
| `shell` | — | COMMAND がシェル（`bash`, `zsh`, `fish`, `sh`, `dash`, `nu`）だけ |
| `dead` | — | `pane_dead == 1` |
| `unknown` | 上記以外・値なし | — |

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
- 制御文字（タブ以外。改行や ESC を含む）を含むメッセージはエラー（終了コード 2）。端末へのエスケープシーケンスの注入と、途中の改行による意図しない送信を防ぐため。
- 長さの上限は 4096 文字（超えたら終了コード 2）。

### 10.4 送り方

外部コマンドはシェルを使わず、一覧と同じ `Runner` で実行する（テストで差し替えられること）。

- Herdr：
  1. `herdr pane send-text <pane_id> <メッセージ>`（メッセージが `-` で始まる場合でも誤解釈されない形にする。実装時に herdr の引数解析で確かめ、必要なら `--` を挟む）
  2. Enter を送る場合：`herdr pane send-keys <pane_id> enter`（キー名は実装時に herdr で確かめる）
- tmux：
  1. `tmux send-keys -t <pane_id> -l -- <メッセージ>`（`-l` で文字どおり送る。キー名として解釈させない）
  2. Enter を送る場合：`tmux send-keys -t <pane_id> Enter`
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
