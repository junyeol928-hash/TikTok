# ttradar — TikTok トレンドレーダー

TikTok で**商品紹介系の動画**を伸ばすために、
「いま何が売れていて、何がこれから伸びるのか」を継続的に監視するツールです。

**ブラウザで開くダッシュボードアプリ**と、cron で回す CLI の両方が入っています。

```bash
ttradar serve      # ← ブラウザでダッシュボードが開く
```

- **機会マップ** — 「競合の少なさ × 伸び率」の散布図。左上の *狙い目ゾーン* が
  「まだ競合が薄いのに伸びている」＝ 今すぐ動画にすべきもの
- **ランキング** — スコア順に一覧。各行にスパークラインと日本語の根拠付き
- **詳細パネル** — クリックで時系列チャート（十字カーソル付き）と全指標
- 種別・ステージ・キーワードで絞り込み、表形式に切替、ダークモード対応
- アプリ内の「収集する」ボタンからそのまま収集できる

---

## このツールの考え方

**「今バズっているもの」を知るだけなら TikTok を眺めれば済みます。**
価値があるのは、**まだ競合が薄いうちに、これから伸びるものを見つける**ことです。

そのため ttradar は絶対値ではなく **変化** を見ます。

| 見るもの | 意味 | 判断 |
|---|---|---|
| 絶対値が大きい | もう遅い | レッドオーシャン |
| 伸び率が高い (1階微分) | 今が旬 | まだ乗れる |
| **加速度がプラス (2階微分)** | **これから伸びる** | **★狙い目** |
| 加速度がマイナス | ピークアウト間近 | 作っても公開時には遅い |

収集したデータは全て時系列で SQLite に蓄積され、実行のたびに差分から
伸び率・加速度を計算し、6 段階のトレンドステージに分類します。

| 表示 | 意味 | アクション |
|---|---|---|
| 🚀 伸び始め | 伸びていて、さらに加速中 | **最優先。今日撮って今日出す** |
| 📈 上昇中 | 順調に伸長中 | まだ十分間に合う |
| 🆕 新規検出 | 初めて観測された | 数回の実行で本物か見極める |
| ➖ 横ばい | 変化なし | 定番ネタとして使える |
| ⚠️ ピーク | 伸びが失速 | 今から作ると遅い |
| 📉 下降 | 減少中 | 新規参入は非推奨 |

> **重要**: 1 回目の実行では「今の順位」しか分かりません。
> 伸び率は**過去との差分**なので、**1 日 2〜3 回の定期実行を数日続けて初めて本領を発揮します。**
> 最初の 2〜3 日はデータを貯める期間だと思ってください。

---

## セットアップ

```bash
git clone https://github.com/junyeol928-hash/TikTok.git
cd TikTok

make install-all        # 仮想環境 + 依存 + Chromium を一括インストール
# 手動でやる場合:
#   python3 -m venv .venv
#   .venv/bin/pip install -e ".[all]"
#   .venv/bin/playwright install chromium

.venv/bin/ttradar init      # config.yaml を生成
.venv/bin/ttradar doctor    # 環境と TikTok への到達性を診断  ← まずこれ
```

### まず動作を体験する (ネットワーク不要)

```bash
.venv/bin/ttradar demo
```

7 日分のサンプル履歴を生成して、収集 → 分析 → ランキング → HTML レポートまで
一通り動かします。実際にどんな出力が出るのかがすぐ分かります。

### アプリを開く

```bash
.venv/bin/ttradar serve            # http://127.0.0.1:8765 が自動で開く
```

`--port` でポート変更、`--no-browser` で自動起動を抑制できます。
既定では **127.0.0.1 のみ** で待ち受けるので、外部からは見えません
(`--host 0.0.0.0` を指定した場合のみ同一 LAN に公開されます)。

### CLI で使う

```bash
.venv/bin/ttradar collect          # 収集して DB に保存
.venv/bin/ttradar report --html    # 分析して HTML レポート出力
.venv/bin/ttradar run              # 収集〜通知まで一括 (定期実行はこれ)
```

---

## ⚠️ ネットワークについての重要な注意

**このリポジトリを作成した開発環境からは `tiktok.com` / `ads.tiktok.com` への
アクセスが組織のネットワークポリシーによりブロックされていました。**
そのため、実際の TikTok からのライブ取得は**未検証**です。

検証済みのもの:

- ✅ 収集 → 保存 → 伸び率計算 → スコアリング → ランキング → HTML/通知 の全パイプライン
- ✅ 実際の Creative Center レスポンス形状を模したデータでのパーサー動作
- ✅ 77 件の自動テスト (時系列判定・正規化・DB・スコアリング・API・統合)
- ✅ アプリ画面の描画確認 (ライト/ダーク/モバイル/表形式)
- ❌ TikTok エンドポイントへの実接続 (環境制限のため不可)

**手元の PC で `ttradar doctor` を実行してください。** 到達性が確認できれば
そのまま `ttradar collect` が動きます。到達できない場合は下の
「うまく取得できないとき」を参照してください。

---

## 収集元 (データソース)

`ttradar sources` で一覧できます。

| 名前 | 取得できるもの | 必要なもの | 特徴 |
|---|---|---|---|
| `creative_center` | ハッシュタグ / 楽曲 / 動画 / 商品 / キーワード / クリエイター | なし | 公式 Creative Center の JSON API を直接叩く。**高速だが仕様変更に弱い** |
| `browser_creative_center` | 同上 | playwright + chromium | 実ブラウザで開き XHR を傍受。**低速だが壊れにくい。定期実行の本命** |
| `ytdlp_watch` | 指定クリエイターの動画 | yt-dlp | 競合の定点観測 |
| `thirdparty` | 商品の実売データなど | 各社 API キー | Kalodata / FastMoss 等の汎用アダプタ |
| `demo` | サンプル | なし | オフライン検証用 |

### なぜブラウザ方式を推すのか

Creative Center の API は署名パラメータ (`user-sign` 等) が不定期に変わります。
HTTP で直叩きする方式はその都度壊れますが、ブラウザ方式は
**TikTok 自身のフロントエンドにリクエストを作らせて JSON を横から読む**ため、
署名仕様が変わっても動き続けます。

`config.yaml` の `sources` は上から順に実行されるので、両方書いておけば
片方が壊れてももう片方が拾います。

詳細は [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) を参照してください。

---

## 主な機能

### 1. スコアリング (何を作るべきか)

商品には専用のスコア軸があります。単に伸びているだけでは不十分だからです。

| 軸 | 既定の重み | 理由 |
|---|---|---|
| 売れ行きの伸び | 30% | 一番の基本 |
| 報酬率 | 20% | 報酬率 3% の商品は伸びていても割に合わない |
| 競合の少なさ | 20% | 紹介動画が 3 桁あるなら先行者利益は無い |
| トレンド段階 | 15% | ピークアウト済みは避ける |
| 価格帯 | 10% | 衝動買いされやすい価格帯 (既定 1,000〜6,000 円) |
| レビュー評価 | 5% | 低評価商品の紹介は信頼を損なう |

重みは `config.yaml` で全て変更できます。

**スコアは必ず日本語の根拠付きで出ます。**
「87点」だけでは判断できませんが、
「販売が急増: 日次 +62% / 報酬率が高い (25%) / 紹介動画がまだ 28 本 (先行者になれる)」
なら動画を作るか決められます。

### 2. 通知 (随時知る)

```yaml
notify_channels: [slack, discord, email, file]
alert_threshold: 70          # このスコア以上を通知
notify_cooldown_hours: 48    # 同じものを 48 時間は再通知しない
```

閾値超えかつ未通知のものだけを送るので、**通知がスパムになりません**。
Webhook を用意しなくても `file` チャンネルで動作確認できます。

### 3. 追跡リスト (競合の定点観測)

全体トレンドだけでなく、**自分と同じニッチの競合が何を投稿して伸びたか**を
追うのが実務では効きます。

```bash
ttradar watch add creator @competitor_handle --note "同ジャンルの競合"
ttradar watch add keyword "美容家電 レビュー"
ttradar watch list
```

`sources` に `ytdlp_watch` を追加すると、登録したクリエイターの直近動画の
再生数が時系列で記録され、「どの動画が伸びたか」が差分で分かります。

### 4. 自分のニッチを加点

```yaml
my_niches: [美容, コスメ, 時短, キッチン]
```

合致するものはスコアを 15% 加点します。全ジャンルのトレンドより
自分が作れるジャンルのトレンドの方が価値が高いためです。

---

## 定期実行

伸び率は履歴があって初めて計算できるので、**定期実行が前提**です。

### cron (推奨)

```bash
crontab -e
```

```cron
# 日本時間 8:00 と 20:00 に実行
0 8,20 * * * cd /path/to/TikTok && .venv/bin/ttradar run >> logs/radar.log 2>&1
```

1 日 2〜3 回で十分です。それ以上増やしても精度は上がらず、
TikTok 側に負荷をかけるだけです。

### GitHub Actions

`.github/workflows/radar.yml` を同梱しています。
ただし **GitHub のランナーから TikTok に到達できるかは保証されません**。
到達できない場合は手元の PC か VPS で cron 実行してください。

---

## コマンド一覧

| コマンド | 説明 |
|---|---|
| `ttradar init` | 設定ファイルを生成 |
| `ttradar doctor` | 環境・依存・TikTok への到達性を診断 |
| `ttradar serve` | **ブラウザで見るダッシュボードを起動** |
| `ttradar demo` | オフラインのサンプルで動作体験 |
| `ttradar collect` | 収集して DB に保存 |
| `ttradar report --html` | 分析して表示 + HTML レポート出力 |
| `ttradar report --json` | JSON で出力 (他ツール連携用) |
| `ttradar report -t product -n 20` | 商品だけ 20 件表示 |
| `ttradar run` | 収集〜通知まで一括 (cron 向け) |
| `ttradar watch` | 追跡リストの管理 |
| `ttradar sources` | 利用可能な収集元の一覧 |
| `ttradar prune` | 古いデータを削除 |

---

## うまく取得できないとき

```bash
ttradar doctor        # まずこれで切り分ける
```

| 症状 | 原因と対処 |
|---|---|
| `ネットワーク制限で到達不可` | 会社/学校のネットワーク、VPN、または実行環境のポリシー。自宅 PC で試す |
| `全候補エンドポイントが失敗` | TikTok 側の仕様変更。`sources` に `browser_creative_center` を追加する |
| `playwright 未インストール` | `pip install playwright && playwright install chromium` |
| 伸び率が全部 `—` | 履歴不足。時間を空けて複数回 `collect` する |
| 商品が 1 件も取れない | TikTok Shop のデータは地域差が大きい。`regions: [US]` も試す。または `thirdparty` を検討 |
| 429 が出る | `request_interval` を上げる (既定 1.2 秒)。下げすぎない |

---

## 注意事項

- 本ツールは TikTok が**公開している**情報を、**個人の分析目的**で取得します。
  過度なリクエストは行わない設計 (既定で 1.2 秒間隔) ですが、
  `request_interval` を極端に下げないでください。
- Creative Center の内部 API は公式ドキュメントのあるものではなく、
  予告なく変更・停止される可能性があります。
- 取得した数値は各ソースの公開値に基づく推定であり、
  TikTok 公式の確定値ではありません。**投資判断や在庫仕入れの唯一の根拠にしないでください。**
- 業務利用・大規模利用を行う場合は、TikTok の利用規約および
  各社 API の契約条件をご確認ください。

---

## 開発

```bash
make test                                  # テスト実行 (77件)
.venv/bin/python -m pytest tests/ -v       # 詳細表示
```

構成:

```
ttradar/
├── cli.py              コマンドライン
├── server.py           ローカル Web アプリ (標準ライブラリのみ / JSON API)
├── config.py           設定 (秘密情報は環境変数からのみ)
├── db.py               SQLite 時系列ストア (append-only)
├── models.py           Snapshot / TrendSignal / TrendStage
├── collectors/         収集元 (プラグイン式)
│   ├── base.py         基底 + 耐変更の正規化ヘルパ
│   ├── creative_center.py
│   ├── browser.py      Playwright で XHR 傍受
│   ├── ytdlp_source.py
│   ├── thirdparty.py   設定駆動の汎用 API アダプタ
│   └── demo.py
├── analysis/
│   ├── metrics.py      ★伸び率・加速度・ステージ判定
│   ├── scoring.py      ★スコアリング + 日本語の根拠生成
│   └── digest.py       パイプライン
├── report/
│   ├── console.py      ターミナル表示
│   ├── html.py         静的 HTML レポート
│   └── templates/
│       ├── app.html    ★ダッシュボードアプリ (外部依存なしの SPA)
│       └── digest.html.j2
└── notify/             Slack / Discord / メール / ファイル
```

新しい収集元を足すには `Collector` を継承して `@register("名前")` を付け、
`collect(region) -> list[Snapshot]` を実装するだけです。

---

## ドキュメント

- [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) — データソースの選択肢と実態
- [docs/PLAYBOOK.md](docs/PLAYBOOK.md) — 出力をどう動画制作に活かすか
