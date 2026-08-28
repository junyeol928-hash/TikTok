"""動画から商品・クリエイター・ハッシュタグを組み立てる.

考え方
------
「どの商品で動画を撮るか」を決めるのに本当に効くのは、
広告レポート的な集計値ではなく **実際に伸びている紹介動画の集合** である。

    その商品を紹介した動画が今週 12 本
    → 合計 340 万再生、中央値 8.2 万再生
    → 保存率 3.8% (= 買う気で見られている)
    → 紹介しているクリエイターはまだ 9 人

ここまで出て初めて「撮るかどうか」が判断できる。
このモジュールは VIDEO スナップショットを入力に、
PRODUCT / CREATOR / HASHTAG のスナップショットを **導出** する。

導出されたものは通常のエンティティとして DB に入るので、
伸び率・加速度・ステージ判定・スコアリングがそのまま効く。

中央値を重視する理由
--------------------
合計再生数だけ見ると、1 本だけバズった商品が上位に来てしまう。
「その商品なら自分が撮っても伸びる」かを知りたいのだから、
**代表的な 1 本がどれくらい伸びるか** = 中央値の方が意思決定に近い。
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Any, Iterable, Sequence

from ..models import EntityType, M, Snapshot
from ..util.log import get

log = get(__name__)

#: 商品名の正規化で落とす飾り
_NOISE = re.compile(r"[【】\[\]（）()｜|/／・,、。!！?？\"'`~＿_＊*#＃]+")
_SPACES = re.compile(r"\s+")

#: 商品名として短すぎる / 一般的すぎるものは商品と見なさない
_STOPWORDS = {"商品", "アイテム", "リンク", "こちら", "詳細", "shop", "tiktok",
              "セール", "クーポン", "割引", "購入", "ショップ", "ここから"}

#: 商品とは関係なく、露出を取るためだけに付けるタグ。
#: 集計から外すと「実際に使われているタグ」ではなくなるので残すが、
#: 商品ジャンルのタグと同列に見えると「#fyp を付けろ」という
#: 中身のない示唆になってしまうため、UI で区別できるよう印を付ける。
REACH_TAGS = {
    "fyp", "fypシ", "fypage", "foryou", "foryoupage", "viral", "trending",
    "tiktok", "tiktokjapan", "capcut", "おすすめ", "おすすめにのりたい",
    "おすすめのりたい", "おすすめに乗りたい", "バズりたい", "伸びろ",
    "拡散希望", "急上昇", "フォロー", "フォローミー", "いいね", "followme",
}


def is_reach_tag(tag: str) -> bool:
    """商品ではなく露出目的のタグか."""
    return str(tag).strip().lstrip("#").lower() in REACH_TAGS


#: 商品名ではなく導線の文言。アンカーのタイトルにこれが入ることがある
_JUNK_PHRASES = re.compile(
    r"(リンクはこちら|詳細はこちら|プロフィール(から|のリンク)|こちらから|"
    r"チェックして|タップして|購入はこちら|お得に|限定クーポン|"
    r"click here|shop now|link in bio|buy now)", re.I)


def normalize_product_name(name: str) -> str:
    """表記ゆれを吸収して同じ商品をまとめる."""
    s = _NOISE.sub(" ", str(name))
    s = _SPACES.sub(" ", s).strip()
    return s


def product_key(name: str) -> str:
    """集計キー. 大文字小文字と空白を無視する."""
    return normalize_product_name(name).lower().replace(" ", "")


def is_valid_product(name: str) -> bool:
    """商品名として使えるか.

    アンカーのタイトルには商品名ではなく「リンクはこちら」のような
    導線文言が入ることがあり、これを商品として集計すると
    無関係な動画が 1 つの偽の商品に合流してしまう。
    """
    n = normalize_product_name(name)
    if len(n) < 3:
        return False
    if _JUNK_PHRASES.search(n):
        return False
    key = product_key(n)
    if key in _STOPWORDS:
        return False
    # 名前がストップワードの寄せ集めでしかない場合も除外
    stripped = key
    for w in _STOPWORDS:
        stripped = stripped.replace(w, "")
    return len(stripped) >= 2


def _median(vals: Sequence[float]) -> float:
    return float(statistics.median(vals)) if vals else 0.0


def _sum(vals: Sequence[float]) -> float:
    return float(sum(vals))


def rollup_products(videos: Iterable[Snapshot], region: str,
                    source: str = "rollup",
                    hit_bar: float | None = None,
                    min_confidence: float = 0.5) -> list[Snapshot]:
    """紹介動画をまとめて PRODUCT スナップショットを作る.

    商品リンク付きの動画だけでなく、キャプションから商品名を取り出せた
    動画も対象にする。日本では Shop リンク付きの動画が少なく、
    リンク必須にすると伸びている紹介動画の大半が捨てられてしまうため。

    どこから取った名前か (``name_source``) と確からしさ (``confidence``) は
    残して UI に出す。推定である以上、利用者が根拠の動画で確かめられる
    ようにしておく必要がある。
    """
    groups: dict[str, list[Snapshot]] = defaultdict(list)
    names: dict[str, str] = {}
    urls: dict[str, str] = {}
    best_src: dict[str, tuple[float, str]] = {}   # key -> (confidence, source)

    for v in videos:
        for cand in _candidates_of(v):
            raw_name = cand.get("name")
            conf = float(cand.get("confidence") or 0)
            if not raw_name or conf < min_confidence:
                continue
            if not is_valid_product(raw_name):
                continue
            key = product_key(raw_name)
            if v not in groups[key]:
                groups[key].append(v)
            # 表示名は最も長いものを採用 (省略された名前より情報が多い)
            disp = normalize_product_name(raw_name)
            if len(disp) > len(names.get(key, "")):
                names[key] = disp
            src = str(cand.get("source") or "caption")
            if conf > best_src.get(key, (0.0, ""))[0]:
                best_src[key] = (conf, src)
            if src == "anchor":
                url = ((v.extra or {}).get("product") or {}).get("url")
                if url and key not in urls:
                    urls[key] = str(url)

    _merge_subsumed(groups, names, best_src)

    out: list[Snapshot] = []
    for key, vids in groups.items():
        snap = _build(EntityType.PRODUCT, key, names[key], vids, region, source,
                      url=urls.get(key), hit_bar=hit_bar)
        conf, src = best_src.get(key, (0.5, "caption"))
        snap.extra["name_source"] = src
        snap.extra["name_confidence"] = round(conf, 2)
        out.append(snap)
    return out


def _candidates_of(v: Snapshot) -> list[dict[str, Any]]:
    """動画が紹介している商品の候補. 旧形式のデータとも互換を保つ."""
    e = v.extra or {}
    cands = e.get("product_candidates")
    if isinstance(cands, list) and cands:
        return [c for c in cands if isinstance(c, dict)]
    # product_candidates が無い古いスナップショット向け
    prod = e.get("product")
    if isinstance(prod, dict) and prod.get("name"):
        return [{"name": prod["name"], "confidence": 1.0, "source": "anchor"}]
    return []


def _merge_subsumed(groups: dict[str, list[Snapshot]], names: dict[str, str],
                    best_src: dict[str, tuple[float, str]]) -> None:
    """「収納ケース」と「ダイソーの収納ケース」を 1 つにまとめる.

    片方の名前がもう片方に完全に含まれ、かつ **同じ動画を根拠にしている**
    ときだけまとめる。名前が似ているだけで別の商品のことはあるので、
    根拠の重なりを条件にする。まとめ先は長い方 (情報が多い方)。
    """
    keys = sorted(groups, key=len, reverse=True)
    for short in list(keys):
        if short not in groups:
            continue
        for long in keys:
            if long == short or long not in groups or short not in groups:
                continue
            if short not in long:
                continue
            a = {id(v) for v in groups[short]}
            b = {id(v) for v in groups[long]}
            if not (a & b):
                continue                      # 根拠が重ならないなら別物
            for v in groups.pop(short):
                if v not in groups[long]:
                    groups[long].append(v)
            names.pop(short, None)
            sc = best_src.pop(short, None)
            if sc and sc[0] > best_src.get(long, (0.0, ""))[0]:
                best_src[long] = sc
            break


def rollup_creators(videos: Iterable[Snapshot], region: str,
                    source: str = "rollup",
                    hit_bar: float | None = None) -> list[Snapshot]:
    """クリエイター別にまとめる. 同じニッチで誰が勝っているかが分かる."""
    groups: dict[str, list[Snapshot]] = defaultdict(list)
    names: dict[str, str] = {}
    followers: dict[str, float] = {}

    for v in videos:
        e = v.extra or {}
        handle = e.get("creator")
        if not handle:
            continue
        groups[str(handle)].append(v)
        names[str(handle)] = str(e.get("creator_name") or handle)
        f = e.get("creator_followers")
        if f:
            followers[str(handle)] = float(f)

    out: list[Snapshot] = []
    for handle, vids in groups.items():
        snap = _build(EntityType.CREATOR, handle, f"@{handle}", vids, region, source,
                      url=f"https://www.tiktok.com/@{handle}", hit_bar=hit_bar)
        snap.extra["nickname"] = names.get(handle)
        if handle in followers:
            snap.metrics[M.FOLLOWERS] = followers[handle]
        out.append(snap)
    return out


def rollup_hashtags(videos: Iterable[Snapshot], region: str,
                    source: str = "rollup", min_videos: int = 2,
                    hit_bar: float | None = None) -> list[Snapshot]:
    """ハッシュタグ別にまとめる.

    Creative Center の汎用トレンドタグより、
    **実際に伸びている商品紹介動画が使っているタグ** の方が実用的。
    """
    groups: dict[str, list[Snapshot]] = defaultdict(list)
    for v in videos:
        for tag in (v.extra or {}).get("hashtags") or []:
            t = str(tag).strip().lstrip("#")
            if t:
                groups[t.lower()].append(v)

    out: list[Snapshot] = []
    for tag, vids in groups.items():
        if len(vids) < min_videos:
            continue          # 1 本しか使っていないタグはノイズ
        snap = _build(EntityType.HASHTAG, tag, f"#{tag}", vids, region, source,
                      url=f"https://www.tiktok.com/tag/{tag}", hit_bar=hit_bar)
        snap.metrics[M.POSTS] = float(len(vids))
        snap.extra["reach_tag"] = is_reach_tag(tag)
        out.append(snap)
    return out


def _build(etype: EntityType, native_id: str, name: str,
           vids: list[Snapshot], region: str, source: str,
           url: str | None = None, hit_bar: float | None = None) -> Snapshot:
    """動画群から 1 つの導出スナップショットを作る (共通処理).

    ``hit_bar`` は「成功」とみなす再生数のライン (全動画の中央値)。
    これを超えた動画の割合 = 再現性の指標になる。
    1 本だけバズった商品と、安定して伸びる商品を区別するために使う。
    """
    views = [v.metrics.get(M.VIEWS, 0.0) for v in vids]
    likes = [v.metrics.get(M.LIKES, 0.0) for v in vids]
    saves = [v.metrics.get(M.SAVES, 0.0) for v in vids]
    engs = [v.metrics[M.ENGAGEMENT_RATE] for v in vids if M.ENGAGEMENT_RATE in v.metrics]
    srates = [v.metrics[M.SAVE_RATE] for v in vids if M.SAVE_RATE in v.metrics]
    vels = [v.metrics[M.VELOCITY] for v in vids if M.VELOCITY in v.metrics]
    creators = {(v.extra or {}).get("creator") for v in vids}
    creators.discard(None)

    metrics: dict[str, float] = {
        # VIEWS は主要ボリューム指標。伸び率はこの値で計算される
        M.VIEWS: _sum(views),
        M.TOTAL_VIEWS: _sum(views),
        M.MEDIAN_VIEWS: _median(views),
        M.VIDEO_COUNT: float(len(vids)),
        M.LIKES: _sum(likes),
        M.SAVES: _sum(saves),
        M.RELATED_VIDEOS: float(len(vids)),      # 競合の多さとしても使う
        M.CREATOR_COUNT: float(len(creators)),
        M.RELATED_CREATORS: float(len(creators)),
    }
    if engs:
        metrics[M.ENGAGEMENT_RATE] = _median(engs)
    if srates:
        metrics[M.SAVE_RATE] = _median(srates)
    if vels:
        # 中央値を使う。合計だと動画本数が多いだけの商品が
        # 「勢いがある」と誤読される (46本の商品が4本の商品に20倍差をつけてしまう)。
        # 知りたいのは「その商品の代表的な1本がどれだけの速さで伸びるか」。
        metrics[M.VELOCITY] = _median(vels)
        metrics[M.TOTAL_VELOCITY] = _sum(vels)
    if hit_bar is not None and views:
        # 全体の中央値を超えた本数の割合。「まぐれの1本」と「安定して伸びる商品」を分ける
        metrics[M.HIT_RATE] = sum(1 for v in views if v >= hit_bar) / len(views)

    # 代表動画 = 再生数が最大のもの。UI でサムネイルと「お手本」を出すのに使う
    best = max(vids, key=lambda v: v.metrics.get(M.VIEWS, 0.0))
    # 「どの動画を参考にその商品が伸びていると判断したか」を見せるための一覧。
    # 商品名がキャプションからの推定である以上、根拠は多めに出す。
    top = sorted(vids, key=lambda v: v.metrics.get(M.VIEWS, 0.0), reverse=True)[:10]

    return Snapshot(
        entity_type=etype,
        native_id=native_id,
        name=name[:120],
        source=source,
        metrics=metrics,
        region=region,
        category=best.category,
        url=url or best.url,
        thumbnail=best.thumbnail,
        extra={
            "derived_from_videos": len(vids),
            "top_videos": [{
                "id": v.native_id, "name": v.name[:100], "url": v.url,
                "thumbnail": v.thumbnail,
                "views": v.metrics.get(M.VIEWS),
                "likes": v.metrics.get(M.LIKES),
                "saves": v.metrics.get(M.SAVES),
                "engagement_rate": v.metrics.get(M.ENGAGEMENT_RATE),
                "creator": (v.extra or {}).get("creator"),
                "age_hours": v.metrics.get(M.AGE_HOURS),
            } for v in top],
            "hashtags": _common_hashtags(vids),
        },
        captured_at=best.captured_at,
    )


def _common_hashtags(vids: list[Snapshot], top_n: int = 8) -> list[dict[str, Any]]:
    """この群でよく使われているハッシュタグ (そのまま真似できる).

    露出目的のタグ (#fyp など) は後ろに回す。
    「この商品の紹介動画では何のタグが使われているか」を知りたいのに、
    どのジャンルにも付く汎用タグが先頭を占めると意味がないため。
    """
    counts: dict[str, int] = defaultdict(int)
    for v in vids:
        for t in set((v.extra or {}).get("hashtags") or []):
            counts[str(t).lstrip("#")] += 1
    ranked = sorted(counts.items(), key=lambda x: (is_reach_tag(x[0]), -x[1]))[:top_n]
    return [{"tag": t, "count": c, "reach": is_reach_tag(t)} for t, c in ranked]


def rollup_all(videos: Sequence[Snapshot], region: str) -> list[Snapshot]:
    """動画から導出できるものを全部作る."""
    if not videos:
        return []
    # 「成功」の基準線は、その回に集めた動画全体の再生数中央値。
    # 絶対値で決め打ちすると、ニッチによって厳しすぎたり緩すぎたりする。
    all_views = [v.metrics.get(M.VIEWS, 0.0) for v in videos]
    hit_bar = _median([v for v in all_views if v > 0]) or None

    out: list[Snapshot] = []
    out += rollup_products(videos, region, hit_bar=hit_bar)
    out += rollup_creators(videos, region, hit_bar=hit_bar)
    out += rollup_hashtags(videos, region, hit_bar=hit_bar)
    log.info("動画 %d 件から %d 件を導出 (商品/クリエイター/タグ)",
             len(videos), len(out))
    return out
