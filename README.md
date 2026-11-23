# agent-sessions

Herdr と tmux で開いているペインを、エージェント名・状態・前面のコマンド・作業ディレクトリとともに表示します。Python 3.11 以上で、インストールせずに使えます。

```sh
./agent-sessions
./agent-sessions --source herdr --agents-only
./agent-sessions --source tmux --format json
./agent-sessions --no-header --width 100
```

出力例

```text
SOURCE  SESSION       PANE  AGENT  STATUS   COMMAND                             CWD
herdr   wsh-2027-pre  1     agy    idle     agy --dangerously-skip-permissions  ~/Work/laiso/wsh-2027-pre
herdr   wsh-2027-pre  2     -      unknown  /usr/bin/bash                       ~/Work/laiso/wsh-2027-pre
tmux    demo          0.0   -      shell    bash                                ~/src/app
```

| オプション | 動作 |
|---|---|
| `--source all\|herdr\|tmux` | 既定は両方。指定したソースのコマンドを PATH から呼び出します。 |
| `--format table\|json` | 既定は表。JSON 配列にはペイン ID、フォーカス状態、短縮前のパスも含みます。 |
| `--agents-only` | エージェント名のあるペインのみ表示します。 |
| `--no-header` | 表の見出しを省きます。 |
| `--width N` | 表の幅を指定します。既定は端末幅で、パイプでは全長を出力します。 |

Herdr はエージェントの内部状態を表示します。tmux の `running` はプロセスの実行中を表します。Linux では tmux ペインの子プロセスの引数を読み、取得できない場合は tmux のコマンド名を表示します。

終了コードは、1ソース以上を読めた場合は `0`、引数エラーは `2`、全ソースが読めなかった場合は `3` です。tmux サーバーの不在は0件として扱います。

テストはリポジトリ直下で `python3 -m unittest discover -s tests` を実行してください。仕様の詳細は [設計書](docs/design.md) にあります。

## ペインにメッセージを送る

`send` は一覧と同じ情報から対象を解決し、そのペイン ID に本文と Enter を送ります。シェルが開いているペインでは、既定の Enter により入力したコマンドが実行されます。

```sh
./agent-sessions send --dry-run herdr:wsh-2027-pre/1 '進捗を教えてください'
./agent-sessions send herdr:wN:pH '進捗を教えてください'
./agent-sessions send tmux:demo echo hello
./agent-sessions send --no-enter tmux:demo:0.0 -- '--help'
./agent-sessions send --dry-run --format json tmux:%12 '次の作業は？'
```

最初の例の出力

```text
Would send to herdr wsh-2027-pre/1 (wN:pH) [agy, idle]
> 進捗を教えてください ⏎
```

Herdr は `herdr:<セッション>/<ペイン>` または `herdr:<ペインID>`、tmux は `tmux:<セッション>`、`tmux:<セッション>:<窓>.<ペイン>`、`tmux:%<ID番号>` で指定します。tmux のセッション名だけを指定すると、そのセッションのアクティブなペインを選びます。名前での指定は接頭辞を省くと両方のソースを検索し、候補が複数あれば一覧を stderr に出します。

本文の引数は空白1つで連結します。ハイフンから始まる本文の前には `--` を置きます。本文は4096文字までで、空白だけの本文と、タブ・改行などの制御文字は使えません（タブはシェルの補完キーとして届くため）。`--no-enter` は入力のみ、`--dry-run` は対象と本文のプレビューです。

`send` の終了コードは成功・プレビューが `0`、引数エラーが `2`、全ソースの読み取り失敗が `3`、対象なし・終了済みペインが `4`、複数候補が `5`、送信失敗が `6` です。
