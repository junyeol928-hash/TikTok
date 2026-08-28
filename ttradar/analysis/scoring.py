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

from ..config import Config, ProductWeights, ScoreWeights, VideoProductWeights
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
    """変化率. 増減が主題なので符号を付ける."""
    return "—" if v is None else f"{v:+.0%}"


def _fmt_rate(v: float | None) -> str:
    """保存率などの割合. 符号は付けず、小数第1位まで出す."""
    return "—" if v is None else f"{v:.1%}"


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
    volume_label: str = "現在値",
    volume_unit: str = "",
    metrics: dict[str, float] | None = None,
) -> tuple[float, list[str]]:
    """ハッシュタグ / 楽曲 / キーワード / 動画向けの汎用スコア.

    戻り値は ``(0-100 のスコア, 日本語の根拠リスト)``。

    ``volume_label`` / ``metrics`` は根拠文を読めるものにするためのもの。
    動画から導出したタグでは主要指標が「紹介動画の本数」なので、
    「現在値 40」ではなく「紹介動画 40 本」と出したい。
    中央値再生や保存率も取れるため、順位の理由として一緒に出す。
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
        reasons.append(f"{volume_label} {_fmt_num(cur)}{volume_unit}")
    if metrics:
        med = metrics.get(M.MEDIAN_VIEWS)
        if med:
            reasons.append(f"代表的な1本が {_fmt_num(med)} 再生")
        srate = metrics.get(M.SAVE_RATE)
        if srate:
            reasons.append(f"保存率 {_fmt_rate(srate)}")
        creators = metrics.get(M.CREATOR_COUNT)
        if creators and creators > 1:
            # クリエイター行では常に 1 になるので情報が無い
            reasons.append(f"投稿者 {creators:.0f} 人")

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



# --------------------------------------------------- 動画から導出した商品のスコア

#: 紹介動画の本数の「おいしい」範囲。
#: 少なすぎる = まだ誰も試していない (売れる保証がない)
#: 多すぎる   = 飽和していて、自分の 1 本が埋もれる
COMPETITION_SWEET_LO = 3
COMPETITION_SWEET_HI = 25
#: これを超えたら明確なレッドオーシャン
COMPETITION_SATURATED = 120


def competition_fit(video_count: float | None) -> tuple[float, str | None]:
    """紹介動画の本数を 0-1 のスコアにする (山型).

    本数は「競合の多さ」であると同時に「売れる証拠」でもある。
    単調減少でも単調増加でもなく、山型にするのが正しい。
    """
    if video_count is None:
        return 0.5, None
    n = float(video_count)
    if n <= 1:
        return 0.30, "紹介動画がまだ 1 本 — 売れる保証はないが先行できる"
    if n < COMPETITION_SWEET_LO:
        return 0.65, f"紹介動画 {n:.0f} 本 — 検証はこれから"
    if n <= COMPETITION_SWEET_HI:
        return 1.0, f"紹介動画 {n:.0f} 本 — 売れる証拠があり、まだ埋もれない"
    if n <= COMPETITION_SATURATED:
        # 25 -> 120 本で 1.0 から 0.3 へなだらかに落とす
        t = (n - COMPETITION_SWEET_HI) / (COMPETITION_SATURATED - COMPETITION_SWEET_HI)
        return 1.0 - 0.7 * t, f"紹介動画 {n:.0f} 本 — 競合が増えてきている"
    return 0.15, f"紹介動画 {n:.0f} 本 — 飽和。今から入っても埋もれる"


def score_video_product(
    growth: GrowthResult,
    stage: TrendStage,
    metrics: dict[str, float],
    median_views_cohort: Sequence[float],
    weights: "VideoProductWeights",
    niche_match: bool = False,
    velocity_cohort: Sequence[float] = (),
) -> tuple[float, list[str]]:
    """実際の紹介動画から導出した商品のスコア.

    「この商品で動画を撮ったら伸びるか」を、実際に投稿されている
    紹介動画の成績から推定する。販売数や報酬率が取れない代わりに、
    **中央値再生数・保存率・再現性** という、より投稿判断に近い軸で測る。
    """
    reasons: list[str] = []
    w = weights

    # --- 代表的な 1 本がどれだけ伸びるか (合計ではなく中央値) ---
    med = metrics.get(M.MEDIAN_VIEWS)
    if med is None:
        med_score = 0.4
    else:
        med_score = percentile_rank(med, median_views_cohort or [med])
        if med >= 100_000:
            reasons.append(f"紹介動画の中央値が {_fmt_num(med)} 再生 — 型として成立している")
        elif med < 5_000:
            reasons.append(f"中央値 {_fmt_num(med)} 再生 — 伸びている動画は一部だけ")

    # --- 保存率: 商品紹介で最も購買意欲に近いシグナル ---
    sr = metrics.get(M.SAVE_RATE)
    if sr is None:
        save_score = 0.4
    else:
        # 保存率 3% で満点扱い。通常 1% 前後、3% 超は「買う気で見られている」
        save_score = min(1.0, sr / 0.03)
        if sr >= 0.03:
            reasons.append(f"保存率 {sr:.1%} — 買う気で見られている")
        elif sr < 0.005:
            reasons.append(f"保存率 {sr:.1%} — 見られてはいるが購買には繋がりにくい")

    # --- 再現性: まぐれの 1 本か、安定して伸びるか ---
    hit = metrics.get(M.HIT_RATE)
    n_vid = metrics.get(M.VIDEO_COUNT)
    if hit is None or not n_vid or n_vid < 2:
        hit_score = 0.45
    else:
        hit_score = hit
        if hit >= 0.6 and n_vid >= 3:
            reasons.append(f"{n_vid:.0f} 本中 {hit:.0%} が平均超え — 再現性が高い")
        elif hit <= 0.25 and n_vid >= 4:
            reasons.append(f"{n_vid:.0f} 本中 {hit:.0%} しか伸びていない — 当たりは一部")

    # --- 競合の本数 (山型) ---
    comp_score, comp_reason = competition_fit(n_vid)
    if comp_reason:
        reasons.append(comp_reason)

    creators = metrics.get(M.CREATOR_COUNT)
    if creators is not None and n_vid and creators <= 2 and n_vid >= 4:
        reasons.append(f"投稿者は {creators:.0f} 人だけ — 一人が量産しているだけの可能性")

    # --- 伸び ---
    # 前回収集との差分が使えるならそれを使う。
    # 収集 1 回目は差分が取れないが、TikTok は各動画の投稿日時を返すので
    # 「再生数 / 投稿からの経過時間」= 時速 が初回から計算できる。
    # これを伸びの代理指標にすることで、1 回目から意味のある順位が出る。
    daily = growth.daily_rate
    vel = metrics.get(M.VELOCITY)
    if daily is None and vel is not None and velocity_cohort:
        growth_score = percentile_rank(vel, velocity_cohort)
        reasons.append(f"代表的な1本が時速 {_fmt_num(vel)} 再生"
                       f"（収集1回目のため前回比はまだ出せません）")
    elif growth.from_zero and daily is None:
        growth_score = 1.0
        reasons.append("今回初めて観測された商品")
    else:
        daily = daily or 0.0
        growth_score = saturating(daily, scale=0.5)
        if daily >= 0.3:
            reasons.append(f"紹介動画の再生が急増: 日次 {_fmt_pct(daily)}")
        elif daily < -0.1:
            reasons.append(f"勢いが落ちている: 日次 {_fmt_pct(daily)}")
        if vel:
            reasons.append(f"代表的な1本が時速 {_fmt_num(vel)} 再生")

    stage_score = {
        TrendStage.EMERGING: 1.0, TrendStage.NEW: 0.85, TrendStage.RISING: 0.85,
        TrendStage.STABLE: 0.5, TrendStage.PEAKING: 0.25, TrendStage.DECLINING: 0.05,
    }.get(stage, 0.5)

    # --- エンゲージ率 ---
    eng = metrics.get(M.ENGAGEMENT_RATE)
    eng_score = 0.4 if eng is None else min(1.0, eng / 0.12)

    total_w = (w.median_views + w.save_rate + w.reproducibility
               + w.competition + w.growth + w.engagement) or 1.0
    base = (
        w.median_views * med_score
        + w.save_rate * save_score
        + w.reproducibility * hit_score
        + w.competition * comp_score
        + w.growth * (0.6 * growth_score + 0.4 * stage_score)
        + w.engagement * eng_score
    ) / total_w

    score = base * 100.0
    if niche_match:
        score *= 1.15
        reasons.append("自分のニッチに合致")

    reasons.append(STAGE_ADVICE.get(stage, ""))
    return max(0.0, min(100.0, score)), [r for r in reasons if r]
