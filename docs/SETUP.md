# セットアップ手順（はじめての方向け）

コマンドを打つのが初めてでも進められるように書いています。
**上から順にコピー＆ペーストするだけ**です。

---

## 必要なもの

| | 説明 |
|---|---|
| パソコン | Windows か Mac |
| Python 3.10 以上 | 無ければ下の手順で入れます |
| 通信環境 | TikTok に接続できるネットワーク（会社/学校の回線だと弾かれることがあります） |

所要時間はおよそ 10 分（うち待ち時間が 5 分ほど）です。

---

## Mac の場合

### 1. ターミナルを開く

`Command (⌘)` + `スペース` を押して **`ターミナル`** と入力し、Enter。
黒か白のウィンドウが開きます。ここに文字を打ち込んでいきます。

### 2. Python があるか確認する

次の行をコピーしてターミナルに貼り付け、Enter を押します。

```bash
python3 --version
```

- `Python 3.10.x` 以上が表示されれば OK。次に進んでください
- `command not found` と出たら <https://www.python.org/downloads/> から
  インストーラを落として入れ、**ターミナルを閉じて開き直して**からもう一度

### 3. ダウンロードして実行する

以下を**まとめてコピー**してターミナルに貼り付け、Enter。

```bash
cd ~
git clone https://github.com/junyeol928-hash/TikTok.git ttradar
cd ttradar
bash setup.sh
```

> `git: command not found` と出たら、画面の指示に従って
> 開発者ツールをインストールしてください（ボタンを押すだけです）。
> 終わったらもう一度同じコマンドを実行します。

数分待つと、診断結果が表示されます。

### 4. アプリを開く

**`start.command` をダブルクリック**するだけです。
ブラウザが自動で開きます。止めるときは `Control` + `C`。

> 初回は「開発元が未確認」と出ることがあります。
> その場合は右クリック →「開く」→「開く」を選んでください。

コマンドで開きたい場合はこちら:

```bash
.venv/bin/ttradar serve --interval 120 --collect-now
```

---

## Windows の場合

### 1. PowerShell を開く

`Windows` キーを押して **`PowerShell`** と入力し、Enter。
青いウィンドウが開きます。

### 2. Python があるか確認する

```powershell
py --version
```

- `Python 3.10.x` 以上が表示されれば OK
- エラーが出たら <https://www.python.org/downloads/> からインストーラを落とし、
  **インストール時に「Add python.exe to PATH」に必ずチェック**を入れてください。
  終わったら PowerShell を開き直します

### 3. ダウンロードして実行する

```powershell
cd $HOME
git clone https://github.com/junyeol928-hash/TikTok.git ttradar
cd ttradar
powershell -ExecutionPolicy Bypass -File setup.ps1
```

> `cd $HOME` はユーザーフォルダ (例: `C:\Users\yeol0`) に移動します。
> デスクトップを指定しないのは、OneDrive を使っていると
> デスクトップの実体が別の場所にあり `Desktop が存在しない` と言われるためです。

> `git` が無いと言われたら <https://git-scm.com/download/win> から入れて、
> PowerShell を開き直してもう一度実行してください。

### 4. アプリを開く

**`start.bat` をダブルクリック**するだけです。
ブラウザが自動で開きます。止めるときは黒いウィンドウで `Ctrl` + `C`。

コマンドで開きたい場合はこちら:

```powershell
.\.venv\Scripts\ttradar.exe serve --interval 120 --collect-now
```

---

## 2 回目以降

セットアップは最初の 1 回だけです。次からは **ダブルクリックするだけ**。

| やりたいこと | Windows | Mac |
|---|---|---|
| アプリを開く | `start.bat` | `start.command` |
| 最新版に更新する | `update.bat` | ターミナルで `cd` してから `git pull` |
| 1回だけ収集して結果を見る | `collect.bat` | `.venv/bin/ttradar collect` |
| 収集が0件のとき原因を調べる | `probe.bat` | `.venv/bin/ttradar probe --visible` |
| もう一度セットアップ | `setup.bat` | ターミナルで `bash setup.sh` |

タスクバーやDockに登録しておくと、次からは 1 クリックで開けます。

> **コマンドで操作する場合の注意**
> PowerShell やターミナルを新しく開くと、必ずユーザーフォルダから始まります。
> `git pull` などはプロジェクトのフォルダの中で実行する必要があるので、
> **最初に `cd ttradar` を実行してください。**
> これを忘れると `not a git repository` と言われます（壊れてはいません）。

## 診断結果の読み方

`setup.sh` / `setup.ps1` の最後に出る **[TikTok への到達性]** が要点です。

### `OK` と出た場合

そのまま使えます。アプリを開いてください。

### `不可` と出た場合

TikTok に接続できていません。よくある原因は次の通りです。

| 原因 | 対処 |
|---|---|
| 会社 / 学校のネットワーク | 自宅の回線や、スマホのテザリングで試す |
| VPN が有効 | VPN を切って再実行 |
| セキュリティソフトの遮断 | 一時的に無効化して確認 |
| TikTok 側の一時的な制限 | 時間を空けて再実行 |

接続できなくても、**画面の確認だけならサンプルデータでできます**。

```bash
# Mac
.venv/bin/ttradar demo
# Windows
.\.venv\Scripts\ttradar.exe demo
```

---

## うまくいかないとき

| 症状 | 対処 |
|---|---|
| `command not found: python3` | Python が未インストール。手順 2 を参照 |
| `command not found: git` | Mac は開発者ツール、Windows は Git for Windows を入れる |
| `このシステムではスクリプトの実行が無効` (Windows) | `powershell -ExecutionPolicy Bypass -File setup.ps1` の形で実行する |
| インストールが途中で止まる | ネットワークを確認し、もう一度 `bash setup.sh` を実行（何度実行しても安全です） |
| `.venv が壊れています` | `.venv` フォルダを削除してからもう一度セットアップ |
| `not a git repository` | フォルダの外にいます。`cd ttradar` してから実行 |
| `ttradar.exe が認識されません` | フォルダが違います。`cd` を余分にしていないか確認 (`pwd` で現在地を表示)。迷ったら `.bat` をダブルクリックするのが確実 |
| `'if' を使用できません` など大量の構文エラー (Windows) | スクリプトが古い可能性。`git pull` してからやり直す |
| 文字が `縺九ｉ` のように化ける (Windows) | 同上。`git pull` で最新版を取得してください |
| 数字が全部 `—` になる | 履歴不足。`--interval` を付けたまま数時間そのままにしておく |

それでも解決しない場合は、**画面に出たエラーメッセージをそのままコピーして**
質問してください。メッセージがあれば原因を特定できます。
