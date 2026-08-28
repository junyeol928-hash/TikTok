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
from .category import is_food
from .metrics import GrowthResult, compute_growth, classify_stage
from .rollup import rollup_all
from .scoring import (filming_verdict, score_generic, score_product,
                      score_video_product)

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
    #: collector 別の「何本見て何本を商品紹介動画として採用したか」
    filter_stats: dict[str, dict[str, Any]] = field(default_factory=dict)

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
    #: 古すぎて対象外にした動画の件数
    excluded_old: int = 0
    #: 食べ物系として対象外にした件数
    excluded_food: int = 0
    #: 何日前までの動画を見ているか (0 で無制限)
    max_video_age_days: float = 0.0
    #: 食べ物を除外しているか
    exclude_food: bool = True

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
        # collector は config.raw を直接読むので、アプリ側で変えた設定を先に流し込む
        for key in self.UI_SETTING_KEYS:
            self.config.raw[key] = self.setting(key, self.config.raw.get(key))
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
            # collector が「何を見て何を採用したか」を残していれば引き取る。
            # アプリ側で「商品紹介動画だけを分析している」ことを示すのに使う。
            stats = getattr(col, "stats", None)
            if isinstance(stats, dict) and stats:
                result.filter_stats[col.name] = dict(stats)
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
            result.filter_stats["rollup"] = {
                "videos": len(videos),
                "with_shop_link": sum(1 for v in videos if (v.extra or {}).get("product")),
                "products": sum(1 for s in all_snaps
                                if s.entity_type == EntityType.PRODUCT
                                and str(s.source).startswith("rollup")),
            }

        result.collected = len(all_snaps)
        if all_snaps:
            result.inserted = self.db.upsert_snapshots(all_snaps)
        result.duration = time.time() - t0
        if result.filter_stats:
            self.db.set_meta("last_filter_stats",
                             {"at": time.time(), "by_source": result.filter_stats})
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
        # 「いま何を対象にしているか」はデータが 1 件も無くても画面に出す。
        # ここを早期 return の後ろに置くと、空の DB で「期間の制限なし」と
        # 嘘の表示になる。
        max_age_h = self.max_video_age_days() * 24.0
        drop_food = self.exclude_food()
        digest.max_video_age_days = self.max_video_age_days()
        digest.exclude_food = drop_food

        # 直近 14 日以内に観測されたものだけを対象にする
        entities = self.db.active_entities(region=region, since=now - 14 * 86400)
        digest.total_entities = len(entities)
        if not entities:
            return digest

        # --- 1 パス目: 履歴を読み、伸び率を計算 ---
        prepared: dict[EntityType, list[dict[str, Any]]] = {}
        for ent in entities:
            etype = EntityType(ent["entity_type"])
            # 食べ物は除外する (物を紹介したい人にとってはノイズでしかなく、
            # 食べ物は再生数が伸びやすいのでランキングを占領してしまう)
            if drop_food and is_food(ent["name"], ent["category"]):
                digest.excluded_food += 1
                continue
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

            # 古い動画はトレンドではない。
            # age_hours は取得時点の値なので、その後の経過分を足して今の年齢にする。
            if max_age_h and etype == EntityType.VIDEO:
                age = metrics.get(M.AGE_HOURS)
                if age is not None:
                    age += max(0.0, (now - float(ent["last_seen"])) / 3600.0)
                    if age > max_age_h:
                        digest.excluded_old += 1
                        continue

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

                # ノイズ除去: 小さすぎるものは無視 (商品は販売数が小さくても価値がある)。
                #
                # ただし動画から導出したものには掛けない。
                # 導出タグの主要指標は「今回の収集に含まれた紹介動画の本数」で、
                # 数十本にしかならないのが正常。ここに min_volume (既定100) を
                # 掛けると導出タグが丸ごと消え、ハッシュタグの画面が常に空になる。
                derived = str(ent["source"]).startswith("rollup")
                if (etype != EntityType.PRODUCT and not derived
                        and cur is not None and cur < self.config.min_volume):
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
                    # 動画から導出したものは主要指標が「紹介動画の本数」。
                    # 「現在値 40」では何のことか分からないので言い換える。
                    label, unit = (
                        ("紹介動画", " 本") if derived and etype == EntityType.HASHTAG
                        else ("フォロワー", " 人") if derived and etype == EntityType.CREATOR
                        else ("現在値", ""))
                    score, reasons = score_generic(
                        growth, stage,
                        volume_cohort=volume_cohort,
                        competition=metrics.get(comp_key),
                        competition_cohort=comp_cohort,
                        age_days=r["age_days"],
                        weights=self.config.weights,
                        niche_match=niche,
                        volume_label=label, volume_unit=unit,
                        metrics=metrics if derived else None,
                    )

                verdict = (filming_verdict(stage, metrics)
                           if etype == EntityType.PRODUCT else None)
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
                    verdict=verdict,
                ))

            signals.sort(key=lambda s: s.score, reverse=True)
            digest.by_type[etype] = signals

        return digest

    # ----------------------------------------------------- 実効設定
    # アプリ上で変えた設定 (DB の ui_settings) を config.yaml より優先する。
    # 利用者が YAML を編集しなくても期間や食べ物の扱いを変えられるようにするため。
    UI_SETTING_KEYS = ("max_video_age_days", "exclude_food")

    def ui_settings(self) -> dict[str, Any]:
        try:
            v = self.db.get_meta("ui_settings")
        except Exception:
            return {}
        return v if isinstance(v, dict) else {}

    def setting(self, key: str, default: Any) -> Any:
        ui = self.ui_settings()
        if key in ui and ui[key] is not None:
            return ui[key]
        return self.config.raw.get(key, default)

    def max_video_age_days(self) -> float:
        """何日前までの動画を分析対象にするか (0 で無制限)."""
        try:
            return max(0.0, float(self.setting("max_video_age_days", 30)))
        except (TypeError, ValueError):
            return 30.0

    def exclude_food(self) -> bool:
        return bool(self.setting("exclude_food", True))

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
