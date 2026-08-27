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
import threading
import time
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analysis.digest import Radar
from .config import Config
from .db import Database
from .models import PRIMARY_METRIC, EntityType, M, TrendStage
from .util.log import get

log = get(__name__)

APP_HTML = Path(__file__).parent / "report" / "templates" / "app.html"

#: 収集ジョブの状態 (UI のボタン表示に使う)
_job_lock = threading.Lock()
_job: dict[str, Any] = {"running": False, "started": 0.0, "finished": 0.0,
                        "result": None, "error": None}


def _job_snapshot() -> dict[str, Any]:
    with _job_lock:
        return dict(_job)


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
            _job.update(running=False, finished=time.time(), result=payload)
    except Exception as e:  # noqa: BLE001
        log.exception("収集ジョブが失敗しました")
        with _job_lock:
            _job.update(running=False, finished=time.time(), error=str(e))


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
            return {
                "region": digest.region,
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
            for s in sigs[:limit]:
                d = s.to_dict()
                d["spark"] = self._spark(db, s.entity_key)
                d["primary_metric"] = PRIMARY_METRIC.get(s.entity_type)
                rows.append(d)
            return {"count": len(sigs), "rows": rows}

    def _spark(self, db: Database, key: str, points: int = 14) -> list[float]:
        """スパークライン用に主要指標の推移を間引いて返す."""
        hist = db.history(key)
        vals = [float(r["primary_value"]) for r in hist if r["primary_value"] is not None]
        if len(vals) <= points:
            return vals
        step = len(vals) / points
        return [vals[min(int(i * step), len(vals) - 1)] for i in range(points)]

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

    def meta(self) -> dict[str, Any]:
        """UI の初期化に必要な静的情報."""
        return {
            "regions": self.cfg.regions,
            "sources": self.cfg.sources,
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
            if u.path == "/api/history":
                key = one("key")
                if not key:
                    return self._error(400, "key が必要です")
                return self._json(self.api.history(key))
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
          open_browser: bool = True) -> None:
    """ローカル Web アプリを起動する."""
    api = Api(cfg)
    handler = partial(Handler, api=api, cfg=cfg)
    httpd = ThreadingHTTPServer((host, port), handler)

    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    print(f"\n  📡 ttradar アプリを起動しました\n\n     {url}\n")
    if host == "0.0.0.0":
        print("  ⚠ 0.0.0.0 で待ち受けています。同じネットワークの他の端末から")
        print("     収集データが見えます。信頼できるネットワークでのみ使用してください。\n")
    print("  終了するには Ctrl+C\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました。")
    finally:
        httpd.server_close()
