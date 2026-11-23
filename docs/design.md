# agent-sessions 設計書 v1

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

## 9. 設計へのフィードバック

実装中に設計と食い違う点・設計が足りない点が見つかったら、このファイルの末尾に「## 変更履歴」を作って追記し、報告に書くこと。

## 変更履歴

- v1 実装時に §4.1 の境界条件を明確化した。0件の表は見出しの指定にかかわらず `No sessions found.` の1行。幅8は COMMAND と CWD を縮める際の下限で、元から短い列は広げない。指定幅に収まらない場合は下限と固定列を優先する。表のセルに含まれる改行・制御文字は空白で表示し、JSON には元の値を保持する。
- §4.1 の Herdr の整列はワークスペース番号、タブ番号を優先する。同じタブ内は表示名、同名ならペイン ID 順。番号がない項目は番号付き項目の後ろに表示する。
- §5.3 の「最後の子」は Linux の `/proc/<pane_pid>/task/<pane_pid>/children` の末尾の PID とする。引数に空白を含むパスも扱えるよう、AGENT 判定には空白連結前の引数を使う。
