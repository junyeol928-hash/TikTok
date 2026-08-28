"""収集 -> 分析 -> シグナル化のパイプライン.

``Radar`` が本システムのオーケストレータ。CLI からはこれだけを呼ぶ。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..collectors.base import Collector, all_collectors, get_collector
from ..config import Config
from ..db import Database
from ..models import (PRIMARY_METRIC, EntityType, M, Snapshot, TrendSignal,
                      TrendStage)
from ..util.log import get
from .metrics import GrowthResult, compute_growth, classify_stage
from .rollup import rollup_all
from .scoring import score_generic, score_product, score_video_product

log = get(__name__)

#: 新規扱いにする経過時間 (これ以内に初検出されたものは NEW 候補)
NEW_WINDOW_HOURS = 36.0


@dataclass
class RunResult:
    """1 回の収集実行の結果."""

    collected: int = 0
    inserted: int = 0
    errors: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.inserted > 0 or not self.errors


@dataclass
class Digest:
    """分析結果. エンティティ種別ごとにランキング済みのシグナルを持つ."""

    generated_at: float = field(default_factory=time.time)
    by_type: dict[EntityType, list[TrendSignal]] = field(default_factory=dict)
    region: str = "JP"
    total_entities: int = 0
    #: 履歴が足りず伸び率を出せなかった件数 (初回実行時はこれが全件になる)
    insufficient_history: int = 0

    def top(self, etype: EntityType, n: int = 10) -> list[TrendSignal]:
        return self.by_type.get(etype, [])[:n]

    def alerts(self, threshold: float) -> list[TrendSignal]:
        """通知に値するシグナルを横断で集める."""
        out: list[TrendSignal] = []
        for sigs in self.by_type.values():
            out.extend(s for s in sigs if s.score >= threshold)
        return sorted(out, key=lambda s: s.score, reverse=True)

    def all_signals(self) -> list[TrendSignal]:
        out: list[TrendSignal] = []
        for sigs in self.by_type.values():
            out.extend(sigs)
        return sorted(out, key=lambda s: s.score, reverse=True)


class Radar:
    """収集と分析のオーケストレータ."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    # ------------------------------------------------------------------ 収集
    def build_collectors(self, sources: Sequence[str] | None = None) -> list[Collector]:
        names = list(sources) if sources else list(self.config.sources)
        built: list[Collector] = []
        for name in names:
            cls = get_collector(name)
            if cls is None:
                log.warning("未知の collector: %s (利用可能: %s)",
                            name, ", ".join(sorted(all_collectors())))
                continue
            try:
                # DB を必要とする collector (watchlist 参照など) に渡す
                try:
                    inst = cls(self.config, db=self.db)  # type: ignore[call-arg]
                except TypeError:
                    inst = cls(self.config)
                built.append(inst)
            except Exception as e:  # noqa: BLE001
                log.warning("collector %s の初期化に失敗: %s", name, e)
        return built

    def collect(self, sources: Sequence[str] | None = None,
                regions: Sequence[str] | None = None) -> RunResult:
        """全 collector を回してスナップショットを DB に保存する."""
        t0 = time.time()
        result = RunResult()
        run_id = self.db.start_run()
        collectors = self.build_collectors(sources)
        regions = list(regions) if regions else list(self.config.regions)

        if not collectors:
            result.errors.append("有効な collector がありません")
            self.db.finish_run(run_id, False, 0, result.errors)
            return result

        all_snaps: list[Snapshot] = []
        for col in collectors:
            for region in regions:
                snaps, err = col.safe_collect(region)
                if err:
                    result.errors.append(err)
                if snaps:
                    all_snaps.extend(snaps)
                    result.by_source[col.name] = result.by_source.get(col.name, 0) + len(snaps)
            try:
                col.close()
            except Exception:
                pass

        # 動画が取れていれば、そこから商品・クリエイター・ハッシュタグを導出する。
        # 「どの商品で撮るか」の判断材料は集計値ではなく実際の紹介動画にあるため、
        # これが本システムで最も価値のある変換になる。
        videos = [s for s in all_snaps if s.entity_type == EntityType.VIDEO]
        if videos:
            for region in regions:
                derived = rollup_all([v for v in videos if v.region == region], region)
                if derived:
                    all_snaps.extend(derived)
                    result.by_source["rollup"] = (
                        result.by_source.get("rollup", 0) + len(derived))

        result.collected = len(all_snaps)
        if all_snaps:
            result.inserted = self.db.upsert_snapshots(all_snaps)
        result.duration = time.time() - t0
        self.db.finish_run(run_id, result.ok, result.inserted, result.errors)
        return result

    # ------------------------------------------------------------------ 分析
    def analyze(self, region: str | None = None,
                window_hours: float | None = None) -> Digest:
        """DB の履歴から伸び率を計算し、ランキング済みのシグナルを返す."""
        region = region or (self.config.regions[0] if self.config.regions else "JP")
        window = window_hours if window_hours is not None else self.config.growth_window_hours
        now = time.time()
        digest = Digest(region=region, generated_at=now)

        # 直近 14 日以内に観測されたものだけを対象にする
        entities = self.db.active_entities(region=region, since=now - 14 * 86400)
        digest.total_entities = len(entities)
        if not entities:
            return digest

        # --- 1 パス目: 履歴を読み、伸び率を計算 ---
        prepared: dict[EntityType, list[dict[str, Any]]] = {}
        for ent in entities:
            etype = EntityType(ent["entity_type"])
            hist = self.db.history(ent["entity_key"], since=now - 30 * 86400)
            if not hist:
                continue
            series = [(float(r["captured_at"]), float(r["primary_value"]))
                      for r in hist if r["primary_value"] is not None]
            if not series:
                continue

            growth = compute_growth(series, window_hours=window, now=now)
            if growth.growth_rate is None and not growth.from_zero:
                digest.insufficient_history += 1

            latest = hist[-1]
            import json as _json
            metrics = _json.loads(latest["metrics"] or "{}")

            age_days = (now - float(ent["first_seen"])) / 86400.0
            is_new = (now - float(ent["first_seen"])) < NEW_WINDOW_HOURS * 3600 and len(series) <= 2

            prepared.setdefault(etype, []).append({
                "ent": ent, "growth": growth, "metrics": metrics,
                "age_days": age_days, "is_new": is_new,
                "current": growth.current,
            })

        # --- 2 パス目: 種別ごとのコホートを作り、相対評価でスコア化 ---
        for etype, rows in prepared.items():
            volume_cohort = [r["current"] for r in rows if r["current"] is not None]
            comp_key = (M.RELATED_VIDEOS if etype == EntityType.PRODUCT
                        else M.RELATED_CREATORS)
            comp_cohort = [r["metrics"].get(comp_key) for r in rows]
            comp_cohort = [c for c in comp_cohort if c is not None]
            median_cohort = [r["metrics"].get(M.MEDIAN_VIEWS) for r in rows]
            median_cohort = [c for c in median_cohort if c is not None]
            vel_cohort = [r["metrics"].get(M.VELOCITY) for r in rows]
            vel_cohort = [c for c in vel_cohort if c is not None]

            signals: list[TrendSignal] = []
            for r in rows:
                ent, growth, metrics = r["ent"], r["growth"], r["metrics"]
                cur = r["current"]

                # ノイズ除去: 小さすぎるものは無視 (商品は販売数が小さくても価値がある)
                if (etype != EntityType.PRODUCT and cur is not None
                        and cur < self.config.min_volume):
                    continue

                stage = classify_stage(growth, is_new=r["is_new"])
                niche = self._matches_niche(ent["name"], ent["category"])

                if etype == EntityType.PRODUCT and str(ent["source"]).startswith("rollup"):
                    # 実際の紹介動画から導出した商品。
                    # 販売数や報酬率は無いが、中央値再生数・保存率・再現性という
                    # より投稿判断に近い軸で測れる。
                    score, reasons = score_video_product(
                        growth, stage, metrics,
                        median_views_cohort=median_cohort,
                        weights=self.config.video_product_weights,
                        niche_match=niche,
                        velocity_cohort=vel_cohort,
                    )
                elif etype == EntityType.PRODUCT:
                    score, reasons = score_product(
                        growth, stage, metrics,
                        sales_cohort=volume_cohort,
                        competition_cohort=comp_cohort,
                        weights=self.config.product_weights,
                        price_sweet_spot=self.config.price_sweet_spot,
                        niche_match=niche,
                    )
                else:
                    score, reasons = score_generic(
                        growth, stage,
                        volume_cohort=volume_cohort,
                        competition=metrics.get(comp_key),
                        competition_cohort=comp_cohort,
                        age_days=r["age_days"],
                        weights=self.config.weights,
                        niche_match=niche,
                    )

                signals.append(TrendSignal(
                    entity_key=ent["entity_key"],
                    entity_type=etype,
                    name=ent["name"],
                    source=ent["source"],
                    region=ent["region"],
                    stage=stage,
                    score=score,
                    current_value=cur,
                    growth_rate=growth.growth_rate,
                    acceleration=growth.acceleration,
                    window_hours=growth.window_hours,
                    reasons=reasons,
                    metrics=metrics,
                    category=ent["category"],
                    url=ent["url"],
                    thumbnail=ent["thumbnail"],
                    is_new=r["is_new"],
                    first_seen=float(ent["first_seen"]),
                ))

            signals.sort(key=lambda s: s.score, reverse=True)
            digest.by_type[etype] = signals

        return digest

    def _matches_niche(self, name: str, category: str | None) -> bool:
        """自分のニッチ (config.my_niches) に合致するか."""
        if not self.config.my_niches:
            return False
        hay = f"{name} {category or ''}".lower()
        return any(n.lower() in hay for n in self.config.my_niches)

    # -------------------------------------------------------------- 通知向け
    def new_alerts(self, digest: Digest, channel: str) -> list[TrendSignal]:
        """まだ通知していない、閾値超えのシグナルを返す."""
        out = []
        for sig in digest.alerts(self.config.alert_threshold):
            if self.db.was_notified(sig.entity_key, channel,
                                    self.config.notify_cooldown_hours):
                continue
            out.append(sig)
        return out

    def mark_alerts_sent(self, signals: Iterable[TrendSignal], channel: str) -> None:
        for sig in signals:
            self.db.mark_notified(sig.entity_key, channel, sig.score)
