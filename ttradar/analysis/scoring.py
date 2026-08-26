"""スコアリング: 「で、結局どれをやればいいのか」を 1 つの数字にする.

方針
----
スコアは **必ず根拠 (reasons) とセットで返す**。
理由の無い 87 点には意味が無く、ユーザーは判断できない。
「販売数が 3 日で +240%」「紹介動画がまだ 18 本」のような
日本語の根拠が付いて初めて、動画を作るかどうかを決められる。

正規化は「同種のエンティティ群の中での相対位置」で行う。
ハッシュタグの投稿数と商品の販売数は絶対値のスケールが全く違うため、
絶対値で正規化すると片方が常に勝ってしまう。
"""

from __future__ import annotations

from typing import Sequence

from ..config import Config, ProductWeights, ScoreWeights
from ..models import M, TrendStage
from .metrics import GrowthResult, log_scale, percentile_rank, saturating

#: ステージごとのスコア倍率。狙い目のフェーズを持ち上げ、遅いものを沈める。
STAGE_MULTIPLIER: dict[TrendStage, float] = {
    TrendStage.EMERGING: 1.25,
    TrendStage.NEW: 1.10,
    TrendStage.RISING: 1.05,
    TrendStage.STABLE: 0.80,
    TrendStage.PEAKING: 0.55,
    TrendStage.DECLINING: 0.30,
}

#: ステージごとの一言アドバイス
STAGE_ADVICE: dict[TrendStage, str] = {
    TrendStage.EMERGING: "加速中。今すぐ作れば先行者になれる",
    TrendStage.NEW: "初検出。様子見しつつ小さく試す価値あり",
    TrendStage.RISING: "上昇中。まだ十分間に合う",
    TrendStage.STABLE: "安定。定番ネタとして使える",
    TrendStage.PEAKING: "失速の兆し。今から作ると出す頃には遅い",
    TrendStage.DECLINING: "下降中。新規参入は非推奨",
}


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.0%}"


def _fmt_num(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e8:
        return f"{v/1e8:.1f}億"
    if v >= 1e4:
        return f"{v/1e4:.1f}万"
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:.0f}"


def score_generic(
    growth: GrowthResult,
    stage: TrendStage,
    volume_cohort: Sequence[float],
    competition: float | None,
    competition_cohort: Sequence[float],
    age_days: float | None,
    weights: ScoreWeights,
    niche_match: bool = False,
) -> tuple[float, list[str]]:
    """ハッシュタグ / 楽曲 / キーワード / 動画向けの汎用スコア.

    戻り値は ``(0-100 のスコア, 日本語の根拠リスト)``。
    """
    w = weights.normalized()
    reasons: list[str] = []

    # --- 伸び率 ---
    daily = growth.daily_rate if growth.daily_rate is not None else 0.0
    if growth.from_zero and growth.daily_rate is None:
        growth_score = 1.0
        reasons.append("ゼロから立ち上がった (新規発生)")
    else:
        growth_score = saturating(daily, scale=0.5)   # +50%/日 で約 0.76
        if daily >= 0.5:
            reasons.append(f"急成長: 日次 {_fmt_pct(daily)}")
        elif daily >= 0.15:
            reasons.append(f"伸び率 日次 {_fmt_pct(daily)}")
        elif daily < -0.05:
            reasons.append(f"減少中: 日次 {_fmt_pct(daily)}")

    # --- 加速度 ---
    # 加速度は「実際に伸びているもの」に対してのみ意味を持つ。
    # ほぼ横ばいの系列では観測ノイズだけで比率が数倍に跳ねるため、
    # 伸び率の大きさを信頼度としてかけ合わせ、ノイズ由来の加速を減衰させる。
    accel_confidence = saturating(max(0.0, daily), scale=0.3)
    if growth.accel_ratio is not None and growth.accel_ratio != float("inf"):
        # 1.0 を中心に 0-1 へ写す。2.0 倍で約 0.79
        r = growth.accel_ratio
        accel_score = saturating(max(0.0, r - 1.0), scale=0.8) if r > 1 else 0.0
        accel_score *= accel_confidence
        if r > 1.3 and accel_confidence > 0.3:
            reasons.append(f"伸びが加速中 (後半は前半の {r:.1f} 倍のペース)")
        elif r < 0.6:
            reasons.append(f"伸びが失速 (前半の {r:.1f} 倍まで低下)")
    elif growth.from_zero:
        accel_score = 1.0
    else:
        accel_score = 0.35 * accel_confidence   # 判定不能。中立よりやや低く置く

    # --- 絶対ボリューム (同種内での相対位置) ---
    cur = growth.current or 0.0
    if volume_cohort:
        volume_score = percentile_rank(cur, volume_cohort)
    else:
        volume_score = log_scale(cur)
    if cur > 0:
        reasons.append(f"現在値 {_fmt_num(cur)}")

    # --- 新しさ ---
    if age_days is None:
        fresh_score = 0.5
    else:
        # 3 日以内は満点、そこから 21 日かけて減衰
        fresh_score = max(0.0, min(1.0, 1.0 - (age_days - 3.0) / 21.0))
        if age_days <= 3:
            reasons.append("直近で出現したばかり")

    # --- 競合の少なさ ---
    if competition is None:
        comp_score = 0.5
    else:
        # 競合が多いほど低スコア
        comp_score = 1.0 - percentile_rank(competition, competition_cohort or [competition])
        if competition_cohort and comp_score > 0.7:
            reasons.append(f"競合が少ない (関連 {_fmt_num(competition)})")
        elif competition_cohort and comp_score < 0.25:
            reasons.append(f"競合過多 (関連 {_fmt_num(competition)})")

    base = (
        w.growth * growth_score
        + w.acceleration * accel_score
        + w.volume * volume_score
        + w.freshness * fresh_score
        + w.competition * comp_score
    )
    score = base * 100.0 * STAGE_MULTIPLIER.get(stage, 1.0)

    if niche_match:
        score *= 1.15
        reasons.append("自分のニッチに合致")

    reasons.append(STAGE_ADVICE.get(stage, ""))
    return max(0.0, min(100.0, score)), [r for r in reasons if r]


def score_product(
    growth: GrowthResult,
    stage: TrendStage,
    metrics: dict[str, float],
    sales_cohort: Sequence[float],
    competition_cohort: Sequence[float],
    weights: ProductWeights,
    price_sweet_spot: tuple[float, float] = (1000.0, 6000.0),
    niche_match: bool = False,
) -> tuple[float, list[str]]:
    """商品用スコア: 「この商品で紹介動画を作るべきか」.

    汎用スコアと分ける理由は、商品には
    報酬率・価格帯・レビュー評価・競合動画数という固有の判断軸があるため。
    伸びているだけの商品は、報酬率が 3% なら作る価値が薄い。
    """
    reasons: list[str] = []
    w = weights

    # --- 売れ行きの伸び ---
    daily = growth.daily_rate if growth.daily_rate is not None else 0.0
    if growth.from_zero and growth.daily_rate is None:
        vel = 1.0
        reasons.append("販売が立ち上がったばかり")
    else:
        vel = saturating(daily, scale=0.4)
        if daily >= 0.3:
            reasons.append(f"販売が急増: 日次 {_fmt_pct(daily)}")
        elif daily >= 0.1:
            reasons.append(f"販売増: 日次 {_fmt_pct(daily)}")
        elif daily < -0.05:
            reasons.append(f"販売減: 日次 {_fmt_pct(daily)}")

    sales = metrics.get(M.SALES)
    if sales:
        reasons.append(f"販売数 {_fmt_num(sales)}")

    # --- 報酬率 ---
    comm = metrics.get(M.COMMISSION_RATE)
    if comm is None:
        comm_score = 0.4
    else:
        # 20% で満点扱い。アフィリでは 10% 台が普通、20% 超は美味しい
        comm_score = min(1.0, comm / 0.20)
        if comm >= 0.20:
            reasons.append(f"報酬率が高い ({comm:.0%})")
        elif comm < 0.08:
            reasons.append(f"報酬率が低い ({comm:.0%})")

    # --- 競合の少なさ ---
    rel = metrics.get(M.RELATED_VIDEOS)
    if rel is None:
        low_comp = 0.5
    else:
        low_comp = 1.0 - percentile_rank(rel, competition_cohort or [rel])
        if rel <= 30:
            reasons.append(f"紹介動画がまだ {_fmt_num(rel)} 本 (先行者になれる)")
        elif low_comp < 0.25:
            reasons.append(f"紹介動画が {_fmt_num(rel)} 本と飽和気味")

    # --- トレンド段階 ---
    stage_score = {
        TrendStage.EMERGING: 1.0, TrendStage.NEW: 0.85, TrendStage.RISING: 0.8,
        TrendStage.STABLE: 0.5, TrendStage.PEAKING: 0.25, TrendStage.DECLINING: 0.05,
    }.get(stage, 0.5)

    # --- 価格帯 ---
    price = metrics.get(M.PRICE)
    lo, hi = price_sweet_spot
    if price is None:
        price_score = 0.5
    elif lo <= price <= hi:
        price_score = 1.0
        reasons.append(f"価格 {_fmt_num(price)}円 は衝動買いされやすい帯")
    elif price < lo:
        # 安すぎると報酬額が小さい
        price_score = 0.55
    else:
        # 高いほど売れにくい。hi の 4 倍で 0 に近づく
        price_score = max(0.1, 1.0 - (price - hi) / (hi * 3.0))
        if price > hi * 2:
            reasons.append(f"価格 {_fmt_num(price)}円 は高単価で購入ハードルが高い")

    # --- 評価 ---
    rating = metrics.get(M.RATING)
    if rating is None:
        rating_score = 0.5
    else:
        rating_score = max(0.0, min(1.0, (rating - 3.0) / 1.8))
        if rating < 4.0:
            reasons.append(f"評価が低め ({rating:.1f}) — 紹介すると信頼を損なうリスク")
        elif rating >= 4.6:
            reasons.append(f"高評価 ({rating:.1f})")

    total_w = (w.sales_velocity + w.commission + w.low_competition
               + w.trend_stage + w.price_fit + w.rating) or 1.0
    base = (
        w.sales_velocity * vel
        + w.commission * comm_score
        + w.low_competition * low_comp
        + w.trend_stage * stage_score
        + w.price_fit * price_score
        + w.rating * rating_score
    ) / total_w

    score = base * 100.0
    if niche_match:
        score *= 1.15
        reasons.append("自分のニッチに合致")

    reasons.append(STAGE_ADVICE.get(stage, ""))
    return max(0.0, min(100.0, score)), [r for r in reasons if r]
