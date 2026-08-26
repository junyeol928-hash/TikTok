"""設定の読み込み.

優先順位: CLI 引数 > 環境変数 > config.yaml > 既定値
API キー等の秘密情報は .env / 環境変数からのみ読む (YAML には書かせない)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util.log import get

log = get(__name__)

DEFAULT_CONFIG_PATHS = ["config.yaml", "config.yml", "~/.config/ttradar/config.yaml"]


@dataclass
class ScoreWeights:
    """スコアリングの重み. ここを変えると「何を美味しいと見なすか」が変わる."""

    growth: float = 0.35          # 伸び率
    acceleration: float = 0.25    # 加速度 (伸びが伸びているか)
    volume: float = 0.15          # 絶対ボリューム (log スケール)
    freshness: float = 0.10       # 新しさ
    competition: float = 0.15     # 競合の少なさ (逆相関)

    def normalized(self) -> "ScoreWeights":
        total = (self.growth + self.acceleration + self.volume
                 + self.freshness + self.competition)
        if total <= 0:
            return ScoreWeights()
        return ScoreWeights(
            growth=self.growth / total,
            acceleration=self.acceleration / total,
            volume=self.volume / total,
            freshness=self.freshness / total,
            competition=self.competition / total,
        )


@dataclass
class ProductWeights:
    """商品スコアの重み. 「どの商品で動画を作るべきか」の判断軸."""

    sales_velocity: float = 0.30      # 売れ行きの伸び
    commission: float = 0.20          # 報酬率
    low_competition: float = 0.20     # 紹介動画がまだ少ない
    trend_stage: float = 0.15         # 上昇フェーズか
    price_fit: float = 0.10           # 衝動買いしやすい価格帯か
    rating: float = 0.05              # レビュー評価 (低いと炎上リスク)


@dataclass
class Config:
    # --- 取得対象 ---
    regions: list[str] = field(default_factory=lambda: ["JP"])
    #: 有効な collector 名
    sources: list[str] = field(default_factory=lambda: [
        "creative_center", "browser_creative_center",
    ])
    entity_types: list[str] = field(default_factory=lambda: [
        "hashtag", "song", "video", "product", "keyword",
    ])
    #: Creative Center の業種フィルタ (空なら全業種)
    industries: list[str] = field(default_factory=list)
    #: 1 種別あたり何件取るか
    limit_per_type: int = 50
    #: 集計期間 (日). Creative Center は 7/30/120 を受け付ける
    period_days: int = 7

    # --- 分析 ---
    #: 伸び率を計算する比較窓 (時間)
    growth_window_hours: float = 24.0
    #: これ未満のボリュームは無視 (ノイズ除去)
    min_volume: float = 100.0
    #: スコアがこれ以上なら通知対象
    alert_threshold: float = 70.0
    #: 同じものを再通知しないクールダウン (時間)
    notify_cooldown_hours: float = 48.0
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    product_weights: ProductWeights = field(default_factory=ProductWeights)
    #: 衝動買いしやすい価格帯 (円). 商品スコアの price_fit に使う
    price_sweet_spot: tuple[float, float] = (1000.0, 6000.0)

    # --- 出力 ---
    top_n: int = 15
    report_dir: str = "reports"
    db_path: str = "data/ttradar.db"
    #: 通知チャンネル (slack / discord / email / file)
    notify_channels: list[str] = field(default_factory=list)

    # --- 動作 ---
    request_interval: float = 1.2
    timeout: float = 25.0
    retries: int = 3
    keep_days: int = 180
    #: ブラウザ collector を headless で動かすか
    headless: bool = True
    #: 自分のニッチ。ここに合致するものを加点する
    my_niches: list[str] = field(default_factory=list)

    # --- 秘密情報 (環境変数からのみ) ---
    slack_webhook: str | None = None
    discord_webhook: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_to: str | None = None
    kalodata_api_key: str | None = None
    fastmoss_api_key: str | None = None
    echotik_api_key: str | None = None
    tiktok_session_cookie: str | None = None

    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 読み込み
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        data: dict[str, Any] = {}
        found: Path | None = None

        candidates = [path] if path else DEFAULT_CONFIG_PATHS
        for cand in candidates:
            if not cand:
                continue
            p = Path(cand).expanduser()
            if p.exists():
                with p.open(encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                found = p
                break

        if path and found is None:
            raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")
        if found:
            log.debug("設定を読み込みました: %s", found)

        cfg = cls()
        cfg.raw = data

        for key, value in data.items():
            if key == "weights" and isinstance(value, dict):
                cfg.weights = ScoreWeights(**{
                    k: float(v) for k, v in value.items()
                    if k in ScoreWeights.__dataclass_fields__
                })
            elif key == "product_weights" and isinstance(value, dict):
                cfg.product_weights = ProductWeights(**{
                    k: float(v) for k, v in value.items()
                    if k in ProductWeights.__dataclass_fields__
                })
            elif key == "price_sweet_spot" and isinstance(value, (list, tuple)) and len(value) == 2:
                cfg.price_sweet_spot = (float(value[0]), float(value[1]))
            elif hasattr(cfg, key):
                setattr(cfg, key, value)
            else:
                log.warning("設定に未知のキーがあります (無視します): %s", key)

        cfg._load_secrets()
        return cfg

    def _load_secrets(self) -> None:
        """秘密情報を環境変数から読む. .env があれば先に読み込む."""
        try:
            from dotenv import load_dotenv

            load_dotenv(override=False)
        except ImportError:
            pass

        env_map = {
            "slack_webhook": "TTRADAR_SLACK_WEBHOOK",
            "discord_webhook": "TTRADAR_DISCORD_WEBHOOK",
            "smtp_host": "TTRADAR_SMTP_HOST",
            "smtp_user": "TTRADAR_SMTP_USER",
            "smtp_password": "TTRADAR_SMTP_PASSWORD",
            "email_to": "TTRADAR_EMAIL_TO",
            "kalodata_api_key": "KALODATA_API_KEY",
            "fastmoss_api_key": "FASTMOSS_API_KEY",
            "echotik_api_key": "ECHOTIK_API_KEY",
            "tiktok_session_cookie": "TIKTOK_SESSION_COOKIE",
        }
        for attr, env in env_map.items():
            val = os.getenv(env)
            if val:
                setattr(self, attr, val)
        if os.getenv("TTRADAR_SMTP_PORT"):
            self.smtp_port = int(os.environ["TTRADAR_SMTP_PORT"])
        if os.getenv("TTRADAR_DB"):
            self.db_path = os.environ["TTRADAR_DB"]

    def enabled_notifiers(self) -> list[str]:
        """設定と秘密情報の両方が揃っているチャンネルだけ返す."""
        out = []
        for ch in self.notify_channels:
            if ch == "slack" and not self.slack_webhook:
                log.warning("slack が有効ですが TTRADAR_SLACK_WEBHOOK が未設定です")
                continue
            if ch == "discord" and not self.discord_webhook:
                log.warning("discord が有効ですが TTRADAR_DISCORD_WEBHOOK が未設定です")
                continue
            if ch == "email" and not (self.smtp_host and self.email_to):
                log.warning("email が有効ですが SMTP 設定が不足しています")
                continue
            out.append(ch)
        return out
