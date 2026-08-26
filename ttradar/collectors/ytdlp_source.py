"""yt-dlp を使った「指名追跡」collector.

Creative Center は *全体の* トレンドしか見せてくれない。
実際に伸びる人がやっているのは **競合の定点観測** —
自分と同じニッチのクリエイターが今週何を投稿し、どれが伸びたかを見ること。

この collector は watchlist に登録されたクリエイターの直近動画を取得し、
再生数を時系列で記録する。次回実行時との差分で「どの動画が伸びたか」が分かる。

必要なもの
----------
    pip install yt-dlp
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

from ..db import Database
from ..models import EntityType, M, Snapshot
from ..util.log import get
from .base import Collector, dedupe, register

log = get(__name__)


@register("ytdlp_watch")
class YtDlpWatchCollector(Collector):
    """watchlist のクリエイター / 動画を yt-dlp で定点観測する."""

    provides = (EntityType.VIDEO, EntityType.CREATOR)
    requires = "yt-dlp (pip install yt-dlp)"

    #: 1 クリエイターあたり何本まで取るか
    max_videos_per_creator = 12

    def __init__(self, config: Any, db: Database | None = None):
        super().__init__(config)
        self.db = db

    def available(self) -> tuple[bool, str]:
        if shutil.which("yt-dlp") is None:
            try:
                import yt_dlp  # noqa: F401
            except ImportError:
                return False, "yt-dlp 未インストール (pip install yt-dlp)"
        if self.db is None:
            return False, "DB 未接続 (watchlist を読めません)"
        if not self.db.list_watch("creator"):
            return False, "watchlist にクリエイターが未登録 (ttradar watch add creator @xxx)"
        return True, "ok"

    def collect(self, region: str) -> list[Snapshot]:
        assert self.db is not None
        handles = [r["value"] for r in self.db.list_watch("creator")]
        captured = time.time()
        out: list[Snapshot] = []

        for handle in handles:
            handle = handle.lstrip("@")
            url = f"https://www.tiktok.com/@{handle}"
            entries = self._fetch(url)
            if not entries:
                continue

            total_views = 0.0
            for e in entries:
                views = float(e.get("view_count") or 0)
                total_views += views
                vid = str(e.get("id") or "")
                if not vid:
                    continue
                metrics = {
                    M.VIEWS: views,
                    M.LIKES: float(e.get("like_count") or 0),
                    M.COMMENTS: float(e.get("comment_count") or 0),
                    M.SHARES: float(e.get("repost_count") or 0),
                }
                if views > 0:
                    eng = metrics[M.LIKES] + metrics[M.COMMENTS] + metrics[M.SHARES]
                    metrics[M.ENGAGEMENT_RATE] = eng / views
                out.append(Snapshot(
                    entity_type=EntityType.VIDEO,
                    native_id=vid,
                    name=(e.get("title") or e.get("description") or vid)[:120],
                    source=self.name,
                    metrics=metrics,
                    region=region,
                    url=e.get("webpage_url") or f"https://www.tiktok.com/@{handle}/video/{vid}",
                    thumbnail=e.get("thumbnail"),
                    extra={"creator": handle, "upload_date": e.get("upload_date")},
                    captured_at=captured,
                ))

            # クリエイター自体も 1 エンティティとして記録 (平均再生数の推移を見る)
            if entries:
                out.append(Snapshot(
                    entity_type=EntityType.CREATOR,
                    native_id=handle,
                    name=f"@{handle}",
                    source=self.name,
                    metrics={
                        M.VIEWS: total_views / len(entries),
                        M.POSTS: float(len(entries)),
                    },
                    region=region,
                    url=url,
                    captured_at=captured,
                ))
        return dedupe(out)

    def _fetch(self, url: str) -> list[dict[str, Any]]:
        """yt-dlp でメタデータのみ取得 (動画本体はダウンロードしない)."""
        cmd = [
            "yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings",
            "--playlist-end", str(self.max_videos_per_creator), url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            # CLI が無い場合は python モジュールとして呼ぶ
            cmd[0:1] = ["python3", "-m", "yt_dlp"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            except Exception as e:
                log.warning("yt-dlp の実行に失敗: %s", e)
                return []
        except subprocess.TimeoutExpired:
            log.warning("yt-dlp がタイムアウトしました: %s", url)
            return []

        if proc.returncode != 0:
            log.warning("yt-dlp が失敗 (%s): %s", url, (proc.stderr or "")[:200])
            return []

        entries = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
