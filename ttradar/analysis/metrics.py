"""伸び率・加速度・トレンドステージの計算.

このモジュールが本システムの中核。
「今バズっているもの」を知るだけなら TikTok を眺めれば済む。
価値があるのは **これからバズるものを、まだ competition が薄いうちに見つける** こと。
そのために見るのは絶対値ではなく 1 階微分 (伸び率) と 2 階微分 (加速度)。

    絶対値が大きい          → もう遅い (レッドオーシャン)
    伸び率が高い            → 今が旬
    加速度がプラス          → これから伸びる ★狙い目
    加速度がマイナス        → ピークアウト間近

全て純粋関数。DB にも設定にも依存しないのでテストしやすい。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..models import TrendStage

#: これ未満の変化率は誤差として無視する
NOISE_FLOOR = 0.02


@dataclass
class GrowthResult:
    """伸びの計算結果."""

    current: float | None = None
    previous: float | None = None
    growth_rate: float | None = None      # 期間内の変化率 (0.35 = +35%)
    acceleration: float | None = None     # 伸び率の変化量
    window_hours: float | None = None
    points_used: int = 0
    #: 直近の日次換算伸び率 (期間が違うものを横並び比較するため)
    daily_rate: float | None = None
    #: 後半の伸び速度 / 前半の伸び速度.
    #: 差分ベースの acceleration は「一定量ずつ増える直線的な成長」でも
    #: 必ず微減するため、失速判定には比率の方が頑健。
    #: >1 なら加速、<1 なら減速、1.0 前後なら等速。
    accel_ratio: float | None = None
    #: 窓の後半だけで見た日次伸び率。加速シグナルの信頼度判定に使う。
    recent_daily_rate: float | None = None
    #: 期間の始点が 0 だった (= ゼロからの立ち上がり) か。
    #: 変化率が数学的に定義できないが、実務上は最も強い上昇シグナル。
    from_zero: bool = False


def _pick_closest(series: Sequence[tuple[float, float]], target_t: float
                  ) -> tuple[float, float] | None:
    """target_t に最も近い観測点を返す."""
    if not series:
        return None
    return min(series, key=lambda p: abs(p[0] - target_t))


def compute_growth(
    series: Sequence[tuple[float, float]],
    window_hours: float = 24.0,
    now: float | None = None,
) -> GrowthResult:
    """時系列 ``[(timestamp, value), ...]`` から伸び率と加速度を計算する.

    ``series`` は古い順でも新しい順でも良い (内部でソートする)。

    加速度は窓を前半・後半に割り、それぞれの伸び率を比較して求める。
    観測点が 3 点未満の場合、加速度は None (判定不能) になる。
    """
    pts = sorted([(float(t), float(v)) for t, v in series if v is not None])
    res = GrowthResult(points_used=len(pts))
    if not pts:
        return res

    now = now if now is not None else pts[-1][0]
    res.current = pts[-1][1]
    if len(pts) == 1:
        return res

    window_s = window_hours * 3600.0
    start_t = now - window_s

    # --- 伸び率: 窓の始点に最も近い点と最新点を比較 ---
    base = _pick_closest(pts, start_t)
    latest = pts[-1]

    # 始点が 0 の場合、変化率は定義できない。
    # ゼロからの立ち上がりはノイズではなく「新しいトレンドの発生」そのものなので、
    # 窓内で最初に 0 を脱した点を基準に取り直し、それも無ければ立ち上がりと明示する。
    if base is not None and base[1] <= 0:
        in_window = [p for p in pts if p[0] >= base[0] and p[1] > 0]
        if len(in_window) >= 2:
            base = in_window[0]
            res.from_zero = True
        elif latest[1] > 0:
            # 直前まで実質ゼロで、今回初めて値が付いた
            res.from_zero = True
            res.previous = 0.0
            res.window_hours = (latest[0] - base[0]) / 3600.0
            base = None

    if base is not None and base[0] < latest[0] and base[1] > 0:
        res.previous = base[1]
        res.growth_rate = (latest[1] - base[1]) / base[1]
        elapsed_h = (latest[0] - base[0]) / 3600.0
        res.window_hours = elapsed_h
        if elapsed_h > 0:
            # 日次換算 (複利ではなく単純比例。短期の比較用途では十分)
            res.daily_rate = res.growth_rate * (24.0 / elapsed_h)

    # --- 加速度: 前半の伸び率と後半の伸び率の差 ---
    mid_t = now - window_s / 2.0
    p_old = _pick_closest(pts, start_t)
    p_mid = _pick_closest(pts, mid_t)
    p_new = latest
    if (p_old and p_mid and p_old[0] < p_mid[0] < p_new[0]
            and p_old[1] > 0 and p_mid[1] > 0):
        g_first = (p_mid[1] - p_old[1]) / p_old[1]
        g_second = (p_new[1] - p_mid[1]) / p_mid[1]
        # 期間長で正規化してから比較 (前半と後半で時間幅が違う場合に対応)
        h1 = max((p_mid[0] - p_old[0]) / 3600.0, 1e-6)
        h2 = max((p_new[0] - p_mid[0]) / 3600.0, 1e-6)
        rate_first = g_first / h1
        rate_second = g_second / h2
        res.recent_daily_rate = rate_second * 24.0
        # 時間あたりでは値が小さくなりすぎるので日次スケールに戻す
        res.acceleration = (rate_second - rate_first) * 24.0
        if abs(rate_first) > 1e-9:
            # ほぼ横ばいの系列では rate_first が極小になり、比率が容易に
            # 数十倍まで跳ねる。実用上 5 倍を超える差は「強い加速」で頭打ちに
            # して良いのでクランプする (ノイズが順位を支配するのを防ぐ)。
            res.accel_ratio = max(-5.0, min(5.0, rate_second / rate_first))
        elif rate_second > 0:
            res.accel_ratio = float("inf")   # 止まっていたものが動き出した
    return res


#: 後半の伸び速度が前半のこの倍率を超えたら「加速している」とみなす
ACCEL_RATIO_UP = 1.15
#: 後半の伸び速度が前半のこの倍率を下回ったら「失速している」とみなす。
#: 直線的な成長 (一定量ずつ増加) は ratio が 0.7-0.9 程度になるため、
#: それを失速と誤判定しないよう 0.6 に置いている。
ACCEL_RATIO_DOWN = 0.60


def classify_stage(
    growth: GrowthResult,
    is_new: bool = False,
    rising_threshold: float = 0.10,
    accel_threshold: float = 0.05,
) -> TrendStage:
    """伸び率と加速度からトレンドの段階を判定する.

    ``rising_threshold`` は「上昇中」とみなす日次伸び率の下限 (0.10 = +10%/日)。

    加速・失速の判定は差分 (``acceleration``) ではなく比率 (``accel_ratio``) で行う。
    差分ベースだと「毎日 +200 件ずつ増える」ような健全な直線成長でも
    伸び *率* は必ず下がるため、ピークアウトと誤判定してしまう。
    """
    if is_new:
        return TrendStage.NEW

    g = growth.daily_rate if growth.daily_rate is not None else growth.growth_rate
    a = growth.acceleration
    ratio = growth.accel_ratio

    # 比率と絶対量の *両方* を要求する。
    # 比率だけだと、ほぼ横ばいの系列 (例: 5000 -> 5020 -> 4990 -> 5010) で
    # 微小なノイズが比率を大きく振らせ、失速や加速と誤判定してしまう。
    # 絶対量だけだと直線成長を失速と誤判定する。両方見れば双方を防げる。
    def accelerating() -> bool:
        if a is not None and abs(a) <= accel_threshold:
            return False        # 変化量が誤差の範囲
        if ratio is not None:
            return ratio > ACCEL_RATIO_UP
        return a is not None and a > accel_threshold

    def decelerating() -> bool:
        if a is not None and abs(a) <= accel_threshold:
            return False        # 変化量が誤差の範囲
        if ratio is not None:
            # 比率が負 = 前半と後半で伸びの向きが逆転した = 明確な失速
            return ratio < ACCEL_RATIO_DOWN
        return a is not None and a < -accel_threshold

    if g is None:
        # ゼロから立ち上がったものは変化率が計算できないが、最も強い上昇シグナル
        if growth.from_zero and (growth.current or 0) > 0:
            return TrendStage.EMERGING
        return TrendStage.NEW if growth.points_used <= 1 else TrendStage.STABLE

    # 明確な下降
    if g < -rising_threshold:
        return TrendStage.DECLINING

    # ゼロからの立ち上がり。この場合は始点が 0 のため加速度が計算できないが、
    # 「無かったものが生まれた」以上に強い上昇シグナルは無いので先に判定する。
    if growth.from_zero and g > 0:
        return TrendStage.EMERGING

    # 加速度が判定できない場合は伸び率のみで判定
    if a is None and ratio is None:
        if g >= rising_threshold:
            return TrendStage.RISING
        if g <= -NOISE_FLOOR:
            return TrendStage.DECLINING
        return TrendStage.STABLE

    if g >= rising_threshold:
        if accelerating():
            # 伸びていて、さらに加速している = 最も美味しい
            return TrendStage.EMERGING
        if decelerating():
            # 伸びてはいるが失速しつつある
            return TrendStage.PEAKING
        return TrendStage.RISING

    if g > NOISE_FLOOR:
        # 伸びはあるが rising_threshold には届かない領域。
        # ここで EMERGING (=「今すぐ作れば先行者」) を出すと、
        # 日次 +5% 程度のものが日次 +70% のものより上位に来てしまう。
        # 「伸び始め」は十分な伸び率を伴って初めて名乗れる。
        return TrendStage.RISING if accelerating() else TrendStage.STABLE

    # ここに来るのは -rising_threshold <= g <= NOISE_FLOOR の範囲 = 横ばい〜微減。
    # 変化が誤差の範囲なら「ピーク」とは呼ばない。
    # ピークアウトとは *伸びていたものが失速すること* であって、
    # 最初から動いていないものはただの横ばい。
    if abs(g) <= NOISE_FLOOR:
        return TrendStage.STABLE

    # 明確に減っているが DECLINING と呼ぶほど急ではない = ピークアウト後の緩やかな下降。
    # 「横ばい」と言うと安全に見えてしまうので、正直に「ピーク」と伝える。
    return TrendStage.PEAKING


# ------------------------------------------------------------------ 正規化補助

def log_scale(value: float | None, floor: float = 1.0, ceil: float = 1e8) -> float:
    """値を対数スケールで 0-1 に正規化する.

    再生数のような桁の違う値を線形に扱うと巨大なものだけが勝ってしまうため、
    対数を取ってから正規化する。
    """
    if value is None or value <= 0:
        return 0.0
    v = max(floor, min(float(value), ceil))
    return (math.log10(v) - math.log10(floor)) / (math.log10(ceil) - math.log10(floor))


def percentile_rank(value: float, cohort: Sequence[float]) -> float:
    """同種エンティティ群の中での相対位置 (0-1) を返す.

    エンティティ種別によって値のスケールが全く違うため、
    絶対値ではなく「仲間内での順位」で正規化する方が頑健。
    """
    if not cohort:
        return 0.5
    below = sum(1 for c in cohort if c < value)
    equal = sum(1 for c in cohort if c == value)
    return (below + 0.5 * equal) / len(cohort)


def saturating(x: float, scale: float = 1.0) -> float:
    """0 以上の値を 0-1 に飽和させる (tanh ベース)."""
    if x <= 0:
        return 0.0
    return math.tanh(x / max(scale, 1e-9))


def centered(x: float, scale: float = 1.0) -> float:
    """正負の値を 0-1 に写す (0 -> 0.5)."""
    return (math.tanh(x / max(scale, 1e-9)) + 1.0) / 2.0
