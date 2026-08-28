"""ローカル Web アプリのサーバー.

設計方針
--------
- **依存を増やさない**: 標準ライブラリの ``http.server`` だけで動く。
  1 人が自分の PC で見るダッシュボードに FastAPI + uvicorn は過剰。
- **127.0.0.1 に限定**: 収集データと設定を露出するため、既定で外部に開かない。
  ``--host 0.0.0.0`` は明示的に指定した場合のみ (警告を出す)。
- **API は JSON、画面は 1 枚の HTML**: フロントは CDN を一切使わない自己完結型。
  オフラインでも動くこと自体が要件 (TikTok が見られない環境でも UI は触れる)。
"""

from __future__ import annotations

import json
import mimetypes
import statistics
import threading
import time
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analysis.digest import Radar
from .collectors.tiktok_video import DEFAULT_QUERIES
from .config import Config
from .db import Database
from .models import PRIMARY_METRIC, EntityType, M, TrendStage
from .util.log import get

log = get(__name__)

APP_HTML = Path(__file__).parent / "report" / "templates" / "app.html"

#: 収集ジョブの状態 (UI のボタン表示に使う)
_job_lock = threading.Lock()
_job: dict[str, Any] = {"running": False, "started": 0.0, "finished": 0.0,
                        "result": None, "error": None,
                        "interval_min": 0, "next_run": 0.0, "runs": 0}


def _job_snapshot() -> dict[str, Any]:
    with _job_lock:
        return dict(_job)


class Scheduler(threading.Thread):
    """一定間隔で収集を回すバックグラウンドスレッド.

    cron を設定しなくても「アプリを開いておけば勝手に貯まる」状態にするためのもの。
    伸び率は履歴の差分でしか出ないので、**定期実行はこのツールの前提**であり、
    それをユーザーの環境構築に依存させないほうが確実に動く。
    """

    def __init__(self, cfg: Config, interval_min: float, run_now: bool = False):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.interval = max(5.0, float(interval_min)) * 60.0
        self.run_now = run_now
        self._stop = threading.Event()

    def run(self) -> None:
        with _job_lock:
            _job["interval_min"] = round(self.interval / 60)
        # 起動直後に 1 回走らせるかは呼び出し側の指定に従う
        wait = 0.0 if self.run_now else self.interval
        while not self._stop.is_set():
            with _job_lock:
                _job["next_run"] = time.time() + wait
            if self._stop.wait(wait):
                break
            try:
                log.info("定期収集を開始します")
                _run_collect(self.cfg)
            except Exception:  # noqa: BLE001 - スレッドを死なせない
                log.exception("定期収集が失敗しました")
            wait = self.interval

    def stop(self) -> None:
        self._stop.set()


def _run_collect(cfg: Config) -> None:
    """収集をバックグラウンドで実行する (UI をブロックしない)."""
    with _job_lock:
        if _job["running"]:
            return
        _job.update(running=True, started=time.time(), finished=0.0,
                    result=None, error=None)
    try:
        with Database(cfg.db_path) as db:
            res = Radar(cfg, db).collect()
        payload = {"collected": res.collected, "inserted": res.inserted,
                   "errors": res.errors, "by_source": res.by_source,
                   "duration": round(res.duration, 1)}
        with _job_lock:
            _job.update(running=False, finished=time.time(), result=payload,
                        runs=_job.get("runs", 0) + 1)
    except Exception as e:  # noqa: BLE001
        log.exception("収集ジョブが失敗しました")
        with _job_lock:
            _job.update(running=False, finished=time.time(), error=str(e))


#: 商品リンクは無いが「商品紹介動画としてかなり確か」とみなす下限。
#: 収集時のしきい値 (min_product_intent, 既定 0.35) より厳しくして、
#: 「レビュー」等の語が 1 つ引っかかっただけの動画と区別する。
STRONG_INTENT = 0.65


class Api:
    """DB を読んで JSON を返す層. HTTP から切り離してテストしやすくする."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------ 集計
    def summary(self, window: float | None, region: str | None) -> dict[str, Any]:
        with Database(self.cfg.db_path) as db:
            digest = Radar(self.cfg, db).analyze(region=region, window_hours=window)
            sigs = digest.all_signals()
            counts = {st.value: 0 for st in TrendStage}
            for s in sigs:
                counts[s.stage.value] += 1
            times = db.distinct_capture_times(2)

            # 「商品紹介動画だけを分析している」ことを画面で確認できるようにする。
            # ここが無いと、ただ伸びている動画を混ぜていないか利用者に分からない。
            vids = digest.by_type.get(EntityType.VIDEO, [])
            shop = strong = 0
            for sig in vids:
                ex = self._latest_extra(db, sig.entity_key)
                if ex.get("product"):
                    shop += 1
                if float(ex.get("product_intent") or 0) >= STRONG_INTENT:
                    strong += 1
            focus = {
                "videos": len(vids),
                "with_shop_link": shop,
                "strong_intent": strong,
                "products": len(digest.by_type.get(EntityType.PRODUCT, [])),
                "queries": self._video_queries(),
                "min_product_intent": self._min_intent(),
                "max_age_days": digest.max_video_age_days,
                "exclude_food": digest.exclude_food,
                "excluded_old": digest.excluded_old,
                "excluded_food": digest.excluded_food,
                "last_run": db.get_meta("last_filter_stats"),
            }

            return {
                "region": digest.region,
                "focus": focus,
                "generated_at": digest.generated_at,
                "total_entities": digest.total_entities,
                "insufficient_history": digest.insufficient_history,
                "snapshot_count": db.snapshot_count(),
                "stage_counts": counts,
                "alerts": sum(1 for s in sigs if s.score >= self.cfg.alert_threshold),
                "alert_threshold": self.cfg.alert_threshold,
                "last_capture": times[0] if times else None,
                "capture_rounds": len(db.distinct_capture_times(500)),
                "sources": self.cfg.sources,
                "type_counts": {et.value: len(v) for et, v in digest.by_type.items()},
            }

    def signals(self, window: float | None, region: str | None,
                etype: str | None, stage: str | None, query: str | None,
                limit: int) -> dict[str, Any]:
        with Database(self.cfg.db_path) as db:
            digest = Radar(self.cfg, db).analyze(region=region, window_hours=window)
            sigs = digest.all_signals()

            if etype and etype != "all":
                sigs = [s for s in sigs if s.entity_type.value == etype]
            if stage and stage != "all":
                sigs = [s for s in sigs if s.stage.value == stage]
            if query:
                q = query.lower()
                sigs = [s for s in sigs
                        if q in s.name.lower() or q in (s.category or "").lower()]

            rows = []
            for sig in sigs[:limit]:
                d = sig.to_dict()
                d["spark"] = self._spark(db, sig.entity_key)
                d["primary_metric"] = PRIMARY_METRIC.get(sig.entity_type)
                # 代表動画・よく使われるタグ・投稿者は UI の主役なので一緒に返す
                d["extra"] = self._latest_extra(db, sig.entity_key)
                rows.append(d)
            return {"count": len(sigs), "rows": rows}

    def _latest_extra(self, db: Database, key: str) -> dict[str, Any]:
        row = db.latest_snapshot(key)
        if row is None:
            return {}
        try:
            return json.loads(row["extra"] or "{}")
        except (ValueError, TypeError):
            return {}

    def _spark(self, db: Database, key: str, points: int = 14) -> list[float]:
        """スパークライン用に主要指標の推移を間引いて返す."""
        hist = db.history(key)
        vals = [float(r["primary_value"]) for r in hist if r["primary_value"] is not None]
        if len(vals) <= points:
            return vals
        step = len(vals) / points
        return [vals[min(int(i * step), len(vals) - 1)] for i in range(points)]

    def videos(self, window: float | None, region: str | None,
               sort: str, query: str | None, limit: int,
               kind: str = "all") -> dict[str, Any]:
        """伸びている商品紹介動画そのものを返す.

        商品を決める前に「どんな動画が伸びているか」を見たい場面のためのビュー。
        並び順を変えられるのが要点: 再生数が多い動画と、
        投稿から日が浅いのに伸びている動画 (時速) では意味が違う。

        ``kind`` で「どこまで商品紹介動画に絞るか」を選べる:

        ``shop``
            TikTok Shop の商品リンクが付いている動画だけ。
            どの商品の紹介動画かが確定しているので最も確実。
        ``strong``
            商品リンクは無いが、キャプションとタグから
            商品紹介らしさが高い (>= 0.65) と判定された動画も含む。
        ``all``
            収集時のしきい値 (min_product_intent) を通った動画すべて。
        """
        with Database(self.cfg.db_path) as db:
            digest = Radar(self.cfg, db).analyze(region=region, window_hours=window)
            vids = digest.by_type.get(EntityType.VIDEO, [])
            rows = []
            for sig in vids:
                d = sig.to_dict()
                d["extra"] = self._latest_extra(db, sig.entity_key)
                rows.append(d)

            # 絞り込み前に内訳を数えておく。UI のチップに件数を出すため。
            counts = {
                "all": len(rows),
                "shop": sum(1 for r in rows if (r["extra"] or {}).get("product")),
                "strong": sum(1 for r in rows
                              if float((r["extra"] or {}).get("product_intent") or 0)
                              >= STRONG_INTENT),
            }
            if kind == "shop":
                rows = [r for r in rows if (r["extra"] or {}).get("product")]
            elif kind == "strong":
                rows = [r for r in rows
                        if float((r["extra"] or {}).get("product_intent") or 0)
                        >= STRONG_INTENT]

            if query:
                q = query.lower()
                rows = [r for r in rows
                        if q in r["name"].lower()
                        or q in str((r["extra"] or {}).get("creator", "")).lower()
                        or any(q in str(t).lower()
                               for t in (r["extra"] or {}).get("hashtags", []))]

            keyed = {
                "views": lambda r: r["metrics"].get(M.VIEWS, 0),
                "likes": lambda r: r["metrics"].get(M.LIKES, 0),
                "saves": lambda r: r["metrics"].get(M.SAVES, 0),
                "velocity": lambda r: r["metrics"].get(M.VELOCITY, 0),
                "engagement": lambda r: r["metrics"].get(M.ENGAGEMENT_RATE, 0),
                "save_rate": lambda r: r["metrics"].get(M.SAVE_RATE, 0),
                "recent": lambda r: -r["metrics"].get(M.AGE_HOURS, 1e9),
                "score": lambda r: r["score"],
            }.get(sort or "views", lambda r: r["metrics"].get(M.VIEWS, 0))
            rows.sort(key=keyed, reverse=True)
            return {"count": len(rows), "counts": counts, "kind": kind,
                    "rows": rows[:limit]}

    def formats(self, window: float | None, region: str | None) -> dict[str, Any]:
        """「どういう商品紹介動画が伸びているか」を型ごとに集計する.

        個々の動画を眺めるだけでは「次に自分が何を撮るか」は決まらない。
        知りたいのは *型* — どの切り口で、どのくらいの長さで撮ると伸びるか。

        2 つの軸で出す:

        切り口
            その動画を見つけた検索語。「正直レビュー」で出てくる動画と
            「購入品紹介」で出てくる動画では伸び方が違う。
        長さ
            15 秒の紹介と 45 秒の紹介では成績が変わる。

        合計ではなく **中央値** で比べる。1 本のバズに引っ張られると
        「その型なら自分も伸びる」の判断材料にならないため。
        """
        with Database(self.cfg.db_path) as db:
            digest = Radar(self.cfg, db).analyze(region=region, window_hours=window)
            vids = []
            for sig in digest.by_type.get(EntityType.VIDEO, []):
                ex = self._latest_extra(db, sig.entity_key)
                vids.append((sig.metrics or {}, ex))

        def agg(name: str, rows: list[tuple[dict, dict]]) -> dict[str, Any] | None:
            views = [r[0].get(M.VIEWS) for r in rows]
            views = [v for v in views if v]
            if not views:
                return None
            srate = [r[0].get(M.SAVE_RATE) for r in rows]
            srate = [v for v in srate if v]
            vel = [r[0].get(M.VELOCITY) for r in rows]
            vel = [v for v in vel if v]
            return {
                "name": name,
                "videos": len(rows),
                "median_views": statistics.median(views),
                "save_rate": statistics.median(srate) if srate else None,
                "velocity": statistics.median(vel) if vel else None,
                "with_shop_link": sum(1 for r in rows if r[1].get("product")),
            }

        by_q: dict[str, list] = {}
        for m, ex in vids:
            q = str(ex.get("query") or "その他")
            by_q.setdefault(q, []).append((m, ex))
        queries = [a for a in (agg(k, v) for k, v in by_q.items()) if a]
        queries.sort(key=lambda a: a["median_views"], reverse=True)

        by_d: dict[str, list] = {}
        for m, ex in vids:
            d = m.get(M.DURATION)
            if not d:
                continue
            label = ("〜15秒" if d < 15 else "15〜30秒" if d < 30
                     else "30〜60秒" if d < 60 else "60秒〜")
            by_d.setdefault(label, []).append((m, ex))
        order = ["〜15秒", "15〜30秒", "30〜60秒", "60秒〜"]
        durations = [a for a in (agg(k, by_d[k]) for k in order if k in by_d) if a]

        return {"total": len(vids), "queries": queries, "durations": durations}

    def history(self, key: str) -> dict[str, Any]:
        """詳細チャート用の完全な時系列."""
        with Database(self.cfg.db_path) as db:
            hist = db.history(key)
            ent = next((e for e in db.active_entities() if e["entity_key"] == key), None)
            series = [{"t": float(r["captured_at"]),
                       "v": float(r["primary_value"]) if r["primary_value"] is not None else None,
                       "metrics": json.loads(r["metrics"] or "{}")}
                      for r in hist]
            return {
                "entity_key": key,
                "name": ent["name"] if ent else key,
                "url": ent["url"] if ent else None,
                "category": ent["category"] if ent else None,
                "entity_type": ent["entity_type"] if ent else None,
                "first_seen": float(ent["first_seen"]) if ent else None,
                "series": series,
            }

    #: アプリから変えられる設定と、その正規化のしかた
    ALLOWED_AGE_DAYS = (7.0, 14.0, 30.0, 60.0, 90.0, 0.0)   # 0 = 無制限

    def get_settings(self) -> dict[str, Any]:
        """アプリ側で変更できる設定の現在値."""
        with Database(self.cfg.db_path) as db:
            r = Radar(self.cfg, db)
            return {
                "max_video_age_days": r.max_video_age_days(),
                "exclude_food": r.exclude_food(),
                "age_choices": list(self.ALLOWED_AGE_DAYS),
            }

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """アプリ側の設定を保存する.

        config.yaml は書き換えず DB に持つ。利用者が YAML を触らずに
        「直近何日を見るか」「食べ物を入れるか」を変えられるようにするため。
        こちらが config.yaml より優先される。
        """
        with Database(self.cfg.db_path) as db:
            cur = db.get_meta("ui_settings") or {}
            if not isinstance(cur, dict):
                cur = {}
            if "max_video_age_days" in payload:
                try:
                    d = max(0.0, float(payload["max_video_age_days"]))
                except (TypeError, ValueError):
                    return {"ok": False, "reason": "期間の指定が不正です"}
                if d not in self.ALLOWED_AGE_DAYS:
                    return {"ok": False, "reason": "選べない期間です"}
                cur["max_video_age_days"] = d
            if "exclude_food" in payload:
                cur["exclude_food"] = bool(payload["exclude_food"])
            db.set_meta("ui_settings", cur)
        return {"ok": True, **self.get_settings()}

    def watchlist(self) -> list[dict[str, Any]]:
        with Database(self.cfg.db_path) as db:
            return [dict(r) for r in db.list_watch()]

    def add_watch(self, kind: str, value: str, note: str | None) -> dict[str, Any]:
        with Database(self.cfg.db_path) as db:
            db.add_watch(kind, value, note)
        return {"ok": True}

    def remove_watch(self, kind: str, value: str) -> dict[str, Any]:
        with Database(self.cfg.db_path) as db:
            db.remove_watch(kind, value)
        return {"ok": True}

    def _video_queries(self) -> list[str]:
        """商品紹介動画を探しに行っている検索語 (UI に出して中身を示す)."""
        raw = self.cfg.raw.get("video_queries")
        qs = [str(q) for q in raw] if isinstance(raw, list) and raw else list(DEFAULT_QUERIES)
        tags = self.cfg.raw.get("video_hashtags")
        if isinstance(tags, list):
            qs += [f"#{str(t).lstrip('#')}" for t in tags]
        return qs

    def _min_intent(self) -> float:
        return float(self.cfg.raw.get("min_product_intent", 0.35))

    def meta(self) -> dict[str, Any]:
        """UI の初期化に必要な静的情報."""
        return {
            "regions": self.cfg.regions,
            "sources": self.cfg.sources,
            "video_queries": self._video_queries(),
            "min_product_intent": self._min_intent(),
            "strong_intent": STRONG_INTENT,
            "max_video_age_days": float(self.cfg.raw.get("max_video_age_days", 60) or 0),
            "exclude_food": bool(self.cfg.raw.get("exclude_food", True)),
            "alert_threshold": self.cfg.alert_threshold,
            "default_window": self.cfg.growth_window_hours,
            "my_niches": self.cfg.my_niches,
            "entity_types": [
                {"value": e.value, "label": lbl} for e, lbl in [
                    (EntityType.PRODUCT, "商品"), (EntityType.HASHTAG, "ハッシュタグ"),
                    (EntityType.KEYWORD, "キーワード"), (EntityType.SONG, "楽曲"),
                    (EntityType.VIDEO, "動画"), (EntityType.CREATOR, "クリエイター"),
                ]],
            "stages": [{"value": s.value, "label": s.label_ja, "emoji": s.emoji}
                       for s in TrendStage],
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "ttradar"

    def __init__(self, *args: Any, api: Api, cfg: Config, **kw: Any):
        self.api = api
        self.cfg = cfg
        super().__init__(*args, **kw)

    # ------------------------------------------------------------------ 応答
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # ローカル専用アプリなので外部からの埋め込みを禁止する
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)

        def one(name: str, default: Any = None) -> Any:
            v = q.get(name)
            return v[0] if v else default

        def fnum(name: str) -> float | None:
            v = one(name)
            try:
                return float(v) if v not in (None, "", "auto") else None
            except ValueError:
                return None

        try:
            if u.path in ("/", "/index.html"):
                if not APP_HTML.exists():
                    return self._error(500, "app.html が見つかりません")
                return self._send(200, APP_HTML.read_bytes(),
                                  "text/html; charset=utf-8")
            if u.path == "/favicon.ico":
                # 外部リソースを持たない方針なので SVG を直接返す
                svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                       b'<rect width="32" height="32" rx="7" fill="#2a78d6"/>'
                       b'<circle cx="16" cy="16" r="4" fill="#fff"/>'
                       b'<circle cx="16" cy="16" r="9" fill="none" stroke="#fff" '
                       b'stroke-width="2" opacity=".55"/></svg>')
                return self._send(200, svg, "image/svg+xml")
            if u.path == "/api/meta":
                return self._json(self.api.meta())
            if u.path == "/api/summary":
                return self._json(self.api.summary(fnum("window"), one("region")))
            if u.path == "/api/signals":
                return self._json(self.api.signals(
                    fnum("window"), one("region"), one("type"), one("stage"),
                    one("q"), int(one("limit", 300) or 300)))
            if u.path == "/api/videos":
                return self._json(self.api.videos(
                    fnum("window"), one("region"), one("sort", "views"),
                    one("q"), int(one("limit", 60) or 60),
                    one("kind", "all")))
            if u.path == "/api/formats":
                return self._json(self.api.formats(fnum("window"), one("region")))
            if u.path == "/api/history":
                key = one("key")
                if not key:
                    return self._error(400, "key が必要です")
                return self._json(self.api.history(key))
            if u.path == "/api/settings":
                return self._json(self.api.get_settings())
            if u.path == "/api/watch":
                return self._json({"rows": self.api.watchlist()})
            if u.path == "/api/job":
                return self._json(_job_snapshot())
            return self._error(404, "not found")
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s が失敗", u.path)
            return self._error(500, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._error(400, "JSON が不正です")

        try:
            if u.path == "/api/collect":
                snap = _job_snapshot()
                if snap["running"]:
                    return self._json({"ok": False, "reason": "すでに収集中です"})
                threading.Thread(target=_run_collect, args=(self.cfg,),
                                 daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/settings":
                return self._json(self.api.save_settings(payload))
            if u.path == "/api/watch":
                kind = payload.get("kind")
                value = (payload.get("value") or "").strip()
                if not kind or not value:
                    return self._error(400, "kind と value が必要です")
                if payload.get("remove"):
                    return self._json(self.api.remove_watch(kind, value))
                return self._json(self.api.add_watch(kind, value, payload.get("note")))
            return self._error(404, "not found")
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s が失敗", u.path)
            return self._error(500, f"{type(e).__name__}: {e}")


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, interval_min: float = 0,
          collect_now: bool = False) -> None:
    """ローカル Web アプリを起動する.

    ``interval_min`` を指定すると、その間隔で収集を自動実行する
    (cron を設定しなくてもアプリを開いておくだけで履歴が貯まる)。
    """
    api = Api(cfg)
    scheduler: Scheduler | None = None
    if interval_min and interval_min > 0:
        scheduler = Scheduler(cfg, interval_min, run_now=collect_now)
        scheduler.start()
    elif collect_now:
        threading.Thread(target=_run_collect, args=(cfg,), daemon=True).start()
    handler = partial(Handler, api=api, cfg=cfg)
    httpd = ThreadingHTTPServer((host, port), handler)

    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    print(f"\n  📡 ttradar アプリを起動しました\n\n     {url}\n")
    if host == "0.0.0.0":
        print("  ⚠ 0.0.0.0 で待ち受けています。同じネットワークの他の端末から")
        print("     収集データが見えます。信頼できるネットワークでのみ使用してください。\n")
    if scheduler is not None:
        print(f"  ⏱ {round(scheduler.interval/60)} 分ごとに自動収集します"
              f"（アプリを開いたままにしてください）\n")
    print("  終了するには Ctrl+C\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました。")
    finally:
        if scheduler is not None:
            scheduler.stop()
        httpd.server_close()
