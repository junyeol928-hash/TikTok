# データソースの実態と選択肢

TikTok のトレンドデータをどこから取るか、という話です。
**結論から言うと「完全に公式・無料・網羅的」な経路は存在しません。**
用途に応じて組み合わせることになります。

---

## 1. TikTok Creative Center 【本ツールの主力・無料】

<https://ads.tiktok.com/business/creativecenter/>

TikTok 公式が広告主向けに無料公開しているトレンド分析ツールです。
**ログイン不要**で以下が見られます。

| 見られるもの | 対応 |
|---|---|
| 急上昇ハッシュタグ (業種別・期間別) | ✅ `creative_center` |
| 急上昇楽曲 / サウンド | ✅ |
| 人気動画 (Top Ads) | ✅ |
| 人気クリエイター | ✅ |
| キーワードインサイト | ✅ |
| 人気商品 (地域により提供有無が異なる) | ✅ (取れる地域のみ) |

### 長所
- 完全無料、認証不要
- **TikTok 公式のデータ**なので数値の信頼性が高い
- 日本 (JP) を含む多数の国に対応

### 短所
- 画面が内部で叩いている JSON API を利用するため、**公式ドキュメントは無い**
- パラメータ名・パス・署名仕様が予告なく変わる
- 商品データの網羅性は地域差が大きい (日本の TikTok Shop は 2025 年開始で歴史が浅い)

### 仕様変更への対策
本ツールは 2 段構えにしています。

1. `creative_center` — HTTP 直叩き。速いが壊れやすい
2. `browser_creative_center` — 実ブラウザで開いて XHR を傍受。遅いが壊れにくい

**2 が重要です。** 署名パラメータを TikTok 自身のフロントエンドに作らせて、
返ってきた JSON を横から読むだけなので、署名仕様が変わっても動き続けます。
`config.yaml` の `sources` に両方書いておけば、片方が壊れても拾えます。

さらにパーサー側も、キー名をハードコードせず
「ありそうなキーを総当たり」する実装 (`pluck` / `find_list`) にしてあるため、
レスポンス構造の小変更程度なら吸収します。

---

## 2. TikTok 公式 Research API 【申請制・実質研究者向け】

<https://developers.tiktok.com/products/research-api/>

学術研究者向けの公式 API です。動画・コメント・ユーザー情報が正規に取れます。

- **長所**: 完全に公式・正規。規約上の懸念が無い
- **短所**: 大学等の所属を求められ、審査があり、**商用利用は不可**
- **個人クリエイターの用途には現実的ではありません**

---

## 3. TikTok Shop Partner API 【出店者向け】

<https://partner.tiktokshop.com/>

- **長所**: 自分のショップの実売データが正確に取れる
- **短所**: **セラーアカウントとアプリ審査が必要**。
  取れるのは基本的に**自分のショップのデータ**であって、市場全体のトレンドではない
- 自分で TikTok Shop に出店している場合のみ有用

---

## 4. サードパーティ分析サービス 【有料・本気でやるならここ】

TikTok Shop の**実売データを面で取る**には、事実上ここしかありません。

| サービス | 特徴 |
|---|---|
| Kalodata | TikTok Shop 特化。商品・ショップ・動画の売上分析。北米/東南アジアに強い |
| FastMoss | 商品ランキング、クリエイター分析。日本語 UI あり |
| EchoTik | 商品・広告のトレンド分析 |
| Shoplus | 商品・ショップランキング |

いずれも**月額課金**です (数十〜数百ドル/月)。
本気で TikTok Shop アフィリエイトをやるなら、月 1 商品でも当たれば回収できる
コストなので検討する価値があります。

### 本ツールでの使い方

各社の API 仕様は非公開かつバラバラなので、**コードにハードコードせず
`config.yaml` で定義を書ける汎用アダプタ**にしてあります。
契約したサービスのドキュメントを見て以下のように書けば動きます。

```yaml
sources: [creative_center, thirdparty]

thirdparty_apis:
  - name: kalodata
    base_url: https://api.example.com/v1
    path: /product/rank
    api_key_env: KALODATA_API_KEY    # 実際のキーは .env に置く
    auth: header                      # header | query | bearer
    auth_name: X-API-KEY
    entity_type: product
    params:
      country: JP
      period: 7
    list_path: data.list              # 省略すると自動探索
    field_map:                        # 先方のキー -> ttradar の正規化キー
      name: productName
      sales: salesCount
      revenue: gmv
      price: price
      commission_rate: commissionRate
      related_videos: videoCount
```

`field_map` で先方のキー名を ttradar の正規化キーに対応づけるだけで、
既存の分析・スコアリング・通知が全てそのまま使えます。

---

## 5. TikTok Web の公開ページ 【非推奨】

検索結果ページやハッシュタグページを直接スクレイピングする方法です。

- **短所**: `msToken` / `X-Bogus` 等の署名が必要で、対策も頻繁に更新される。
  ブロックされやすく、規約上のリスクも相対的に高い
- 本ツールでは**主力経路として採用していません**

ただし `yt-dlp` 経由で**特定のクリエイター/動画のメタデータ**を取るのは
比較的安定しているため、`ytdlp_watch` として競合の定点観測に使っています。
全体トレンドの探索ではなく「決め打ちの追跡」用途です。

---

## 推奨構成

### 無料で始める
```yaml
sources: [creative_center, browser_creative_center]
```
全体トレンド (ハッシュタグ・楽曲・キーワード・人気動画) はこれで十分取れます。

### 競合追跡を足す
```yaml
sources: [creative_center, browser_creative_center, ytdlp_watch]
```
```bash
ttradar watch add creator @competitor1
ttradar watch add creator @competitor2
```

### 商品の実売データまで本気で見る
```yaml
sources: [creative_center, browser_creative_center, thirdparty]
```
有料サービスを契約し、上記の `thirdparty_apis` を設定します。

---

## 規約とマナーについて

- 本ツールは TikTok が**公開している**情報を、**個人の分析目的**で取得する前提です
- 既定で同一ホストへのリクエスト間隔を 1.2 秒空けています。
  `request_interval` を極端に下げないでください
- 取得データの**再配布・販売**は行わないでください
- 業務利用・大規模利用の際は TikTok の利用規約および各社 API の契約条件を確認してください
- 取得値は公開値に基づく推定です。**在庫仕入れ等の唯一の根拠にしないでください**
