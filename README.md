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
