"""伸び率・加速度・ステージ判定のテスト.

このモジュールが本システムの心臓部なので、想定される時系列の形を
網羅的に固定しておく。ここが壊れるとランキング全体が信用できなくなる。
"""

import time

import pytest

from ttradar.analysis.metrics import (ACCEL_RATIO_DOWN, ACCEL_RATIO_UP,
                                      compute_growth, classify_stage,
                                      log_scale, percentile_rank, saturating)
from ttradar.models import TrendStage


def series(values, hours_span=48.0, now=None):
    """等間隔の時系列を作るヘルパ."""
    now = now or time.time()
    n = len(values)
    if n == 1:
        return [(now, values[0])]
    step = hours_span * 3600 / (n - 1)
    return [(now - (n - 1 - i) * step, v) for i, v in enumerate(values)]


# --------------------------------------------------------------- ステージ判定

@pytest.mark.parametrize("label,values,expected", [
    # 指数的に加速 -> 最も美味しいフェーズ
    ("爆発", [1000, 1400, 2100, 3400], TrendStage.EMERGING),
    # 一定量ずつ増える直線成長。伸び *率* は必ず微減するが失速ではない
    ("直線", [1000, 1200, 1400, 1600], TrendStage.RISING),
    # 伸びてはいるが明確に鈍化
    ("鈍化", [1000, 1600, 2000, 2150], TrendStage.PEAKING),
    ("頭打ち", [1000, 1800, 2000, 2010], TrendStage.PEAKING),
    ("急降下", [4000, 3200, 2600, 2100], TrendStage.DECLINING),
    ("崩壊", [9000, 8000, 4000, 1000], TrendStage.DECLINING),
    # 観測ノイズだけの横ばい。ピークと誤判定してはいけない
    ("横ばい", [5000, 5020, 4990, 5010], TrendStage.STABLE),
    ("微増ノイズ", [100, 98, 103, 101], TrendStage.STABLE),
    ("全ゼロ", [0, 0, 0, 0], TrendStage.STABLE),
    # 緩やかだが継続的な減少はピークアウト扱い (「横ばい」だと安全に見えてしまう)
    ("微減", [1000, 995, 960, 940], TrendStage.PEAKING),
    # 止まっていたものが動き出した
    ("復活", [1000, 1010, 1300, 2000], TrendStage.EMERGING),
    # ゼロからの立ち上がり = 最も強い上昇シグナル
    ("ゼロ始まり", [0, 0, 50, 400], TrendStage.EMERGING),
    ("ゼロ→初値", [0, 0, 0, 120], TrendStage.EMERGING),
    ("ゼロから減", [0, 500, 300, 100], TrendStage.DECLINING),
    # 2 点だけでは加速度が出せないので伸び率のみで判定
    ("2点", [1000, 1500], TrendStage.RISING),
    ("1点", [500], TrendStage.NEW),
])
def test_stage_classification(label, values, expected):
    g = compute_growth(series(values), window_hours=48)
    assert classify_stage(g) is expected, f"{label}: {g}"


def test_noise_driven_acceleration_is_not_emerging():
    """ほぼ横ばいの系列でノイズにより比率が跳ねても EMERGING にしない.

    実データで観測された退行ケース。日次 +5% しか伸びていないのに
    比率が 2.5 倍になり、日次 +70% のものより上位に来てしまっていた。
    """
    g = compute_growth(series([29638, 29810, 31655, 34363]), window_hours=48)
    assert g.accel_ratio is not None and g.accel_ratio > ACCEL_RATIO_UP
    # 比率は高いが伸び率が小さいので「伸び始め」を名乗らせない
    assert classify_stage(g) is TrendStage.RISING


def test_accel_ratio_is_clamped():
    """ほぼ静止した系列から動き出しても比率は上限で頭打ちにする."""
    g = compute_growth(series([1000, 1000.01, 1200, 5000]), window_hours=48)
    assert g.accel_ratio is not None
    assert -5.0 <= g.accel_ratio <= 5.0


# ------------------------------------------------------------------ 伸び率計算

def test_growth_rate_basic():
    g = compute_growth(series([100, 200], hours_span=24), window_hours=24)
    assert g.growth_rate == pytest.approx(1.0)
    assert g.current == 200
    assert g.previous == 100


def test_daily_rate_normalizes_window():
    """窓の長さが違っても日次換算で比較できること."""
    g48 = compute_growth(series([100, 200], hours_span=48), window_hours=48)
    assert g48.daily_rate == pytest.approx(0.5)   # +100% を 2 日で -> 日次 +50%


def test_empty_and_single_point():
    assert compute_growth([]).current is None
    g = compute_growth(series([42]))
    assert g.current == 42
    assert g.growth_rate is None
    assert g.points_used == 1


def test_from_zero_flag():
    g = compute_growth(series([0, 0, 0, 120]), window_hours=48)
    assert g.from_zero is True
    g2 = compute_growth(series([10, 20, 30, 40]), window_hours=48)
    assert g2.from_zero is False


def test_unsorted_input_is_handled():
    """入力の順序に依存しないこと."""
    now = time.time()
    asc = series([100, 150, 300], now=now)
    desc = list(reversed(asc))
    assert (compute_growth(asc, now=now).growth_rate
            == pytest.approx(compute_growth(desc, now=now).growth_rate))


# -------------------------------------------------------------------- 正規化

def test_log_scale_monotonic():
    vals = [log_scale(v) for v in (10, 1e3, 1e5, 1e7)]
    assert vals == sorted(vals)
    assert 0.0 <= vals[0] and vals[-1] <= 1.0
    assert log_scale(0) == 0.0
    assert log_scale(None) == 0.0


def test_percentile_rank():
    cohort = [1, 2, 3, 4, 5]
    assert percentile_rank(0, cohort) == 0.0
    assert percentile_rank(6, cohort) == 1.0
    assert percentile_rank(3, cohort) == pytest.approx(0.5)
    assert percentile_rank(5, []) == 0.5      # コホートが無い場合は中立


def test_saturating_bounds():
    assert saturating(-1) == 0.0
    assert 0.0 < saturating(0.5) < 1.0
    assert saturating(1e6) == pytest.approx(1.0)
