"""ttradar のコマンドラインインターフェース.

    ttradar init            設定ファイルを作る
    ttradar doctor          動作環境と到達性を診断する
    ttradar collect         トレンドを収集して DB に保存
    ttradar report          分析してランキング表示 / HTML 出力
    ttradar run             collect + report + notify (定期実行はこれ)
    ttradar probe           TikTok から実際に何が返るか調べる (0件のとき用)
    ttradar serve           ブラウザで見るダッシュボードアプリを起動
    ttradar watch           追跡リスト (競合クリエイター等) の管理
    ttradar demo            オフラインのサンプルデータで一通り体験する
    ttradar sources         利用可能な収集元を一覧
    ttradar prune           古いデータを掃除
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .db import Database
from .models import EntityType
from .util.log import get, setup

# collector をレジストリに登録するため副作用 import が必要
from .collectors import (base, browser, creative_center,  # noqa: F401
                         demo as demo_mod, thirdparty, tiktok_video,
                         ytdlp_source)

log = get(__name__)

CONFIG_TEMPLATE = """\
# ttradar 設定ファイル
# 秘密情報 (Webhook URL / API キー) はここに書かず .env か環境変数に置くこと。

regions: [JP]                 # 対象国。US, GB なども可 (Creative Center の対応国)

sources:                      # 収集元。上から順に実行される
  - tiktok_video              # ★主力: TikTok本体から商品紹介動画を集める
                              #   ここから商品・クリエイター・タグを自動で導出する
  - browser_creative_center   # 補助: Creative Center を実ブラウザで開いて傍受
                              #   (HTTP直叩きより遅いが仕様変更に強い)
  # - creative_center         # 同じものを HTTP で直叩き。速いが失敗しやすい
  # - ytdlp_watch             # watchlist のクリエイターを定点観測
  # - thirdparty              # 有料分析サービス (下の thirdparty_apis を参照)

entity_types: [hashtag, song, video, product, keyword]

# --- 商品紹介動画をどう探すか (tiktok_video 用) ---
video_queries:                # TikTok 検索に投げるキーワード
  - 購入品紹介
  - 買ってよかった
  - 正直レビュー
  - おすすめ商品
  - 便利グッズ
  - 神アイテム
video_hashtags: []            # ハッシュタグページも見に行く (例: [購入品紹介, 便利グッズ])
min_product_intent: 0.35      # 商品紹介らしさがこれ未満の動画は集計から除外

limit_per_type: 50            # 1 種別あたりの取得件数
period_days: 7                # Creative Center の集計期間 (7 / 30 / 120)

# --- 分析 ---
growth_window_hours: 24       # 伸び率を測る比較窓
min_volume: 100               # これ未満の小さすぎるものは無視
alert_threshold: 70           # このスコア以上を通知
notify_cooldown_hours: 48     # 同じものを再通知しない時間

weights:                      # ハッシュタグ/楽曲/キーワード用の重み
  growth: 0.35                #   伸び率
  acceleration: 0.25          #   加速度 (伸びが伸びているか)
  volume: 0.15                #   絶対ボリューム
  freshness: 0.10             #   新しさ
  competition: 0.15           #   競合の少なさ

video_product_weights:        # ★動画から導出した商品の重み (通常はこちらが使われる)
  median_views: 0.28          #   代表的な1本がどれだけ伸びるか (合計ではなく中央値)
  save_rate: 0.20             #   保存率 = 購買意欲
  reproducibility: 0.18       #   まぐれの1本ではないか
  competition: 0.18           #   紹介動画の本数 (少なすぎも多すぎもNG)
  growth: 0.10                #   前回からの伸び
  engagement: 0.06            #   エンゲージ率

product_weights:              # 販売数・報酬率が取れる場合の重み (thirdparty 等)
  sales_velocity: 0.30        #   売れ行きの伸び
  commission: 0.20            #   報酬率
  low_competition: 0.20       #   紹介動画がまだ少ない
  trend_stage: 0.15           #   上昇フェーズか
  price_fit: 0.10             #   衝動買いしやすい価格帯か
  rating: 0.05                #   レビュー評価

price_sweet_spot: [1000, 6000]   # 衝動買いされやすい価格帯 (円)

# 自分のニッチ。ここに合致するものはスコアを 15% 加点する
my_niches: []
# 例: my_niches: [美容, コスメ, 時短, キッチン]

# --- 出力 ---
top_n: 15
report_dir: reports
db_path: data/ttradar.db
notify_channels: []           # slack / discord / email / file
# 例: notify_channels: [slack, file]

# --- 動作 ---
request_interval: 1.2         # 同一ホストへの最小リクエスト間隔 (秒). 下げすぎない
# ブラウザを画面に表示せずに動かすか。
# false を推奨。TikTok は非表示ブラウザからの検索結果を返さないことがあり、
# 実機では true だと 0 件、false だと取得できることを確認している。
# true にすると収集中に画面が出ないが、0 件になる可能性がある。
headless: false
keep_days: 180

# --- 有料分析サービスを使う場合 (任意) ---
# thirdparty_apis:
#   - name: kalodata
#     base_url: https://api.example.com/v1
#     path: /product/rank
#     api_key_env: KALODATA_API_KEY
#     auth: header
#     auth_name: X-API-KEY
#     entity_type: product
#     params: {country: JP, period: 7}
#     field_map:
#       name: productName
#       sales: salesCount
#       price: price
#       commission_rate: commissionRate
#       related_videos: videoCount
"""


def _db(cfg: Config) -> Database:
    return Database(cfg.db_path)


# ---------------------------------------------------------------- コマンド実装

def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.output)
    if path.exists() and not args.force:
        print(f"既に存在します: {path} (上書きするには --force)")
        return 1
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    print(f"設定ファイルを作成しました: {path}")
    print("次は `ttradar doctor` で環境を確認してください。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """環境診断. ネットワーク制限のある環境で何が使えるかを切り分ける."""
    cfg = Config.load(args.config)
    print("=== ttradar doctor ===\n")

    print(f"[設定] DB: {cfg.db_path} / 対象国: {', '.join(cfg.regions)}")
    print(f"       収集元: {', '.join(cfg.sources)}")
    print(f"       通知: {', '.join(cfg.notify_channels) or '(なし)'}\n")

    print("[依存ライブラリ]")
    for mod, why in [("requests", "必須"), ("yaml", "必須"), ("jinja2", "HTMLレポート"),
                     ("rich", "見やすい表示"), ("playwright", "ブラウザ収集"),
                     ("yt_dlp", "クリエイター定点観測")]:
        try:
            __import__(mod)
            print(f"  OK   {mod:<12} ({why})")
        except ImportError:
            print(f"  なし {mod:<12} ({why})")

    print("\n[収集元]")
    from .collectors.base import all_collectors
    for name, cls in sorted(all_collectors().items()):
        try:
            inst = cls(cfg)
        except TypeError:
            with _db(cfg) as d:
                inst = cls(cfg, db=d)
        ok, why = inst.available()
        mark = "OK  " if ok else "不可"
        print(f"  {mark} {name:<26} {why}")
        if cls.requires:
            print(f"       必要: {cls.requires}")

    print("\n[TikTok への到達性]")
    from .util.http import BlockedError, HttpClient
    client = HttpClient(min_interval=0.2, timeout=15, retries=1)
    reachable = True
    for label, url in [
        ("Creative Center", "https://ads.tiktok.com/business/creativecenter/"),
        ("TikTok 本体", "https://www.tiktok.com/"),
    ]:
        try:
            client.get_text(url)
            print(f"  OK   {label}")
        except BlockedError as e:
            reachable = False
            print(f"  不可 {label} — {str(e)[:100]}")
        except Exception as e:
            reachable = False
            print(f"  不可 {label} — {type(e).__name__}: {str(e)[:80]}")
    client.close()

    if not reachable:
        print("\n  ⚠ TikTok に到達できません。以下のいずれかです:")
        print("     - 実行環境のネットワークポリシーでブロックされている")
        print("     - 社内プロキシ / ファイアウォールの制限")
        print("     - TikTok 側の一時的な制限")
        print("     手元の PC で実行するか、`ttradar demo` でオフライン検証してください。")

    with _db(cfg) as db:
        print(f"\n[データベース] エンティティ {db.entity_count()} 件 / "
              f"スナップショット {db.snapshot_count()} 件")
        times = db.distinct_capture_times(5)
        if times:
            import datetime as _dt
            print("  直近の収集: " + ", ".join(
                _dt.datetime.fromtimestamp(t).strftime("%m/%d %H:%M") for t in times))
        else:
            print("  まだデータがありません。`ttradar collect` を実行してください。")
    return 0 if reachable else 2


def _apply_browser_flags(cfg: Config, args: argparse.Namespace) -> None:
    """--visible / --headless を設定より優先させる.

    利用者が既に生成済みの config.yaml を編集しなくても切り替えられるようにする。
    TikTok は非表示ブラウザに検索結果を返さないことがあるため、
    ここを手早く変えられることが実用上重要。
    """
    if getattr(args, "visible", False):
        cfg.headless = False
    elif getattr(args, "headless", False):
        cfg.headless = True


def cmd_collect(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    _apply_browser_flags(cfg, args)
    sources = args.source or None
    with _db(cfg) as db:
        from .analysis.digest import Radar
        radar = Radar(cfg, db)
        res = radar.collect(sources=sources, regions=args.region or None)

    print(f"\n収集 {res.collected} 件 / 新規保存 {res.inserted} 件 "
          f"({res.duration:.1f}秒)")
    for src, n in sorted(res.by_source.items(), key=lambda x: -x[1]):
        print(f"  {src:<28} {n:>5} 件")
    _print_filter_funnel(res)
    if res.errors:
        print("\n[エラー]")
        for e in res.errors:
            print(f"  - {e}")
    if res.inserted == 0 and res.errors:
        print("\n何も取得できませんでした。`ttradar doctor` で原因を確認してください。")
        return 1
    return 0


def _print_filter_funnel(res: "RunResult") -> None:
    """何を見て何を残したかを端末にも出す.

    アプリを開かなくても「商品が 0 件なのはなぜか」がここで分かるようにする。
    """
    col = res.filter_stats.get("tiktok_video")
    roll = res.filter_stats.get("rollup")
    if not col:
        return
    print("\n[商品紹介動画の絞り込み]")
    print(f"  検索でヒット              {col.get('seen', 0):>5} 件")
    for key, label in (("skipped_not_product", "商品紹介ではない"),
                       ("skipped_old", "古すぎる"),
                       ("skipped_food", "食べ物系")):
        n = col.get(key) or 0
        if n:
            print(f"    - {label:<20} {n:>5} 件を除外")
    print(f"  分析対象の紹介動画        {col.get('kept', 0):>5} 件")
    print(f"    うち商品リンク付き      {col.get('with_shop_link', 0):>5} 件")
    if roll:
        print(f"  導出できた商品            {roll.get('products', 0):>5} 件")

    if not col.get("with_shop_link"):
        print("\n  商品リンク付きの動画が 0 件でした。")
        print("  商品は TikTok Shop の商品リンクが付いた紹介動画からだけ作るため、")
        print("  この状態だと「今撮るべき商品」は出せません。次を試してください:")
        print("    1. config.yaml の video_queries に自分のジャンルの語を足す")
        print("       (例: コスメ 購入品 / ガジェット レビュー / 収納グッズ)")
        print("    2. video_hashtags に [tiktokshop, 購入品紹介] を入れる")
        print("    3. ttradar probe --visible で TikTok が何を返しているか確認する")
        print("    4. 0 件が続くなら login.bat (ttradar login) でログインする")
        print("  動画・ハッシュタグ・クリエイターの分析はこの状態でも使えます。")


def cmd_login(args: argparse.Namespace) -> int:
    """TikTok にログインした状態をブラウザに覚えさせる.

    TikTok は未ログインだと検索結果を返さないことがある。
    Cookie を DevTools から手で写すのは現実的でないので、
    保存されるプロフィールでブラウザを開き、そこでログインしてもらう。
    一度やれば以降の収集でもその状態が使われる。
    """
    cfg = Config.load(args.config)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright が入っていません。先に setup を実行してください。")
        return 1
    from .collectors.tiktok_video import open_browser

    cfg.headless = False          # ログインするので必ず表示する
    print("\nTikTok をブラウザで開きます。")
    print("  1. 開いた画面で、いつも使っているアカウントでログインしてください")
    print("  2. ログインできたら、このウィンドウで Enter を押してください")
    print("     (ブラウザは自動で閉じます)\n")
    with sync_playwright() as pw:
        ctx = open_browser(pw, cfg)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.tiktok.com/login", timeout=60_000)
        except Exception as e:  # noqa: BLE001
            print(f"ページを開けませんでした: {e}")
        try:
            input("ログインが終わったら Enter: ")
        except (EOFError, KeyboardInterrupt):
            pass
        logged = False
        try:
            names = {c["name"] for c in ctx.cookies()}
            logged = "sessionid" in names or "sessionid_ss" in names
        except Exception:  # noqa: BLE001
            pass
        ctx.close()

    if logged:
        print("\nログイン状態を保存しました。次から収集に使われます。")
        return 0
    print("\nログインが確認できませんでした。もう一度お試しください。")
    print("(ログインしなくても収集は動きますが、0 件になることがあります)")
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    with _db(cfg) as db:
        from .analysis.digest import Radar
        from .report import console
        radar = Radar(cfg, db)
        digest = radar.analyze(region=args.region, window_hours=args.window)

        if args.type:
            wanted = {EntityType(t) for t in args.type}
            digest.by_type = {k: v for k, v in digest.by_type.items() if k in wanted}

        if args.json:
            import json
            print(json.dumps([s.to_dict() for s in digest.all_signals()],
                             ensure_ascii=False, indent=2, default=str))
        else:
            console.render(digest, top_n=args.top or cfg.top_n)

        if args.html:
            from .report.html import write_report
            path = write_report(digest, cfg.report_dir, top_n=args.top or cfg.top_n,
                                sources=", ".join(cfg.sources))
            print(f"HTML レポート: {path}")
            print(f"最新版:        {Path(cfg.report_dir) / 'latest.html'}")
        db.record_signals(digest.all_signals())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """定期実行用: 収集 -> 分析 -> レポート -> 通知 を一気通貫で."""
    cfg = Config.load(args.config)
    _apply_browser_flags(cfg, args)
    with _db(cfg) as db:
        from .analysis.digest import Radar
        from .notify import dispatch
        from .report import console
        from .report.html import write_report

        radar = Radar(cfg, db)
        res = radar.collect(sources=args.source or None)
        print(f"収集 {res.collected} 件 / 新規 {res.inserted} 件")
        for e in res.errors:
            log.warning(e)

        digest = radar.analyze(region=args.region)
        console.render(digest, top_n=cfg.top_n)
        db.record_signals(digest.all_signals())

        path = write_report(digest, cfg.report_dir, top_n=cfg.top_n,
                            sources=", ".join(cfg.sources))
        print(f"HTML レポート: {path}")

        channels = cfg.enabled_notifiers()
        if not channels:
            print("通知チャンネルが未設定のため通知はスキップしました。")
            return 0

        for channel in channels:
            fresh = radar.new_alerts(digest, channel)
            console.render_alerts(fresh, cfg.alert_threshold)
            if not fresh:
                continue
            from .notify import get_notifier
            fn = get_notifier(channel)
            if fn and fn(cfg, fresh, "TikTok トレンド速報"):
                radar.mark_alerts_sent(fresh, channel)
                print(f"{channel} に {len(fresh)} 件通知しました。")
            else:
                print(f"{channel} への通知に失敗しました。")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """収集が 0 件のときに、実際の通信を記録して原因を切り分ける."""
    cfg = Config.load(args.config)
    from .probe import run_probe

    query = args.query or (cfg.raw.get("video_queries") or ["購入品紹介"])[0]
    return run_probe(cfg, query=query, visible=args.visible,
                     seconds=args.seconds, out_dir=args.out or cfg.report_dir)


def cmd_serve(args: argparse.Namespace) -> int:
    """ローカル Web アプリを起動する."""
    cfg = Config.load(args.config)
    _apply_browser_flags(cfg, args)
    from .server import serve

    with _db(cfg) as db:
        n = db.entity_count()
    if n == 0:
        print("※ DB が空です。アプリの「収集する」ボタン、または別ターミナルで")
        print("   `ttradar collect` を実行してください。")
        print("   オフラインで見た目を確認するなら `ttradar demo` が先です。\n")

    serve(cfg, host=args.host, port=args.port, open_browser=not args.no_browser,
          interval_min=args.interval, collect_now=args.collect_now)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    with _db(cfg) as db:
        if args.action == "add":
            if not args.value:
                print("追加する値を指定してください (例: ttradar watch add creator @user)")
                return 1
            db.add_watch(args.kind, args.value, args.note)
            print(f"追加しました: [{args.kind}] {args.value}")
        elif args.action == "remove":
            db.remove_watch(args.kind, args.value or "")
            print(f"削除しました: [{args.kind}] {args.value}")
        else:
            rows = db.list_watch(args.kind if args.kind != "all" else None)
            if not rows:
                print("追跡リストは空です。")
                print("例: ttradar watch add creator @competitor_handle")
                return 0
            print(f"{'種別':<10} {'値':<30} メモ")
            for r in rows:
                print(f"{r['kind']:<10} {r['value']:<30} {r['note'] or ''}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """オフラインのサンプルデータで一通りの流れを体験する."""
    cfg = Config.load(args.config)
    cfg.sources = ["demo"]
    if args.db:
        cfg.db_path = args.db
    with _db(cfg) as db:
        from .analysis.digest import Radar
        from .collectors.demo import DemoCollector
        from .report import console
        from .report.html import write_report

        print(f"{args.days} 日分のサンプル履歴を生成中…")
        for off in range(args.days, -1, -1):
            snaps = DemoCollector(cfg, day_offset=float(off)).collect("JP")
            db.upsert_snapshots(snaps)
        print(f"エンティティ {db.entity_count()} 件 / スナップショット {db.snapshot_count()} 件\n")

        radar = Radar(cfg, db)
        digest = radar.analyze(region="JP", window_hours=args.window)
        console.render(digest, top_n=args.top)
        path = write_report(digest, cfg.report_dir, top_n=args.top, sources="demo")
        print(f"HTML レポート: {path}")
        print("\n※ これはサンプルデータです。実データは `ttradar collect` で取得します。")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    from .collectors.base import all_collectors
    print(f"{'名前':<28} {'取得できる種別':<48} 必要なもの")
    print("-" * 110)
    for name, cls in sorted(all_collectors().items()):
        types = ", ".join(e.value for e in cls.provides)
        print(f"{name:<28} {types:<48} {cls.requires}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    with _db(cfg) as db:
        before = db.snapshot_count()
        deleted = db.prune(keep_days=args.keep_days or cfg.keep_days)
        print(f"{deleted} 件のスナップショットを削除しました "
              f"({before} -> {db.snapshot_count()})")
    return 0


# ------------------------------------------------------------------ パーサー

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ttradar",
        description="TikTok の伸びている商品・トレンドを継続的に監視するツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "よくある使い方:\n"
            "  ttradar init                   設定ファイルを作る\n"
            "  ttradar doctor                 環境を診断する\n"
            "  ttradar demo                   オフラインで動作を体験する\n"
            "  ttradar probe --visible        収集が0件のとき原因を調べる\n"
            "  ttradar serve                  ブラウザでダッシュボードを開く\n"
            "  ttradar run                    収集〜通知まで一括 (cron 向け)\n"
            "  ttradar report --html          最新の分析を HTML で出力\n"
        ),
    )
    p.add_argument("--config", "-c", help="設定ファイルのパス")
    p.add_argument("--verbose", "-v", action="store_true", help="詳細ログ")
    p.add_argument("--version", action="version", version=f"ttradar {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="設定ファイルを生成する")
    s.add_argument("--output", "-o", default="config.yaml")
    s.add_argument("--force", "-f", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="環境と到達性を診断する")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("collect", help="トレンドを収集して保存する")
    s.add_argument("--source", "-s", action="append", help="使う収集元 (複数可)")
    s.add_argument("--region", "-r", action="append", help="対象国 (複数可)")
    s.add_argument("--visible", action="store_true",
                   help="ブラウザを表示して収集する (TikTokが非表示を弾く場合に必要)")
    s.add_argument("--headless", action="store_true",
                   help="ブラウザを表示せずに収集する (0件になる可能性あり)")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("report", help="分析結果を表示する")
    s.add_argument("--region", "-r", help="対象国")
    s.add_argument("--window", "-w", type=float, help="比較窓 (時間)")
    s.add_argument("--top", "-n", type=int, help="表示件数")
    s.add_argument("--type", "-t", action="append",
                   choices=[e.value for e in EntityType], help="種別で絞る")
    s.add_argument("--html", action="store_true", help="HTML レポートも出力")
    s.add_argument("--json", action="store_true", help="JSON で出力")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("run", help="収集〜通知まで一括実行 (定期実行向け)")
    s.add_argument("--source", "-s", action="append")
    s.add_argument("--region", "-r")
    s.add_argument("--visible", action="store_true",
                   help="ブラウザを表示して収集する (TikTokが非表示を弾く場合に必要)")
    s.add_argument("--headless", action="store_true",
                   help="ブラウザを表示せずに収集する (0件になる可能性あり)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("probe",
                       help="TikTok から実際に何が返るか調べる (収集が0件のとき)")
    s.add_argument("--query", "-q", help="調べる検索キーワード")
    s.add_argument("--visible", action="store_true",
                   help="ブラウザを画面に表示する (ログイン壁や確認画面が見える)")
    s.add_argument("--seconds", type=float, default=25.0,
                   help="ページを開いてから記録する秒数")
    s.add_argument("--out", help="結果の書き出し先")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("login",
                       help="TikTok にログインした状態を覚えさせる (0件のとき)")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("serve", help="ブラウザで見るダッシュボードを起動する")
    s.add_argument("--port", "-p", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1",
                   help="既定は 127.0.0.1 (自分の PC のみ). 0.0.0.0 は同一 LAN に公開されるので注意")
    s.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    s.add_argument("--interval", type=float, default=0, metavar="分",
                   help="この分数ごとに自動収集する (例: --interval 120 で2時間ごと)。"
                        "cron を設定しなくても履歴が貯まる")
    s.add_argument("--collect-now", action="store_true",
                   help="起動直後に1回収集する")
    s.add_argument("--visible", action="store_true",
                   help="ブラウザを表示して収集する (TikTokが非表示を弾く場合に必要)")
    s.add_argument("--headless", action="store_true",
                   help="ブラウザを表示せずに収集する (0件になる可能性あり)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("watch", help="追跡リストを管理する")
    s.add_argument("action", choices=["add", "remove", "list"], nargs="?", default="list")
    s.add_argument("kind", nargs="?", default="all",
                   choices=["creator", "keyword", "product", "hashtag", "all"])
    s.add_argument("value", nargs="?")
    s.add_argument("--note")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("demo", help="オフラインのサンプルデータで体験する")
    s.add_argument("--days", type=int, default=7, help="生成する履歴の日数")
    s.add_argument("--window", type=float, default=72.0, help="比較窓 (時間)")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--db", help="使用する DB パス (既定の DB を汚したくない場合)")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("sources", help="利用可能な収集元を一覧する")
    s.set_defaults(func=cmd_sources)

    s = sub.add_parser("prune", help="古いデータを削除する")
    s.add_argument("--keep-days", type=int)
    s.set_defaults(func=cmd_prune)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup(verbose=getattr(args, "verbose", False))
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n中断しました。")
        return 130
    except FileNotFoundError as e:
        print(f"エラー: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        log.exception("予期しないエラー")
        print(f"\nエラー: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
