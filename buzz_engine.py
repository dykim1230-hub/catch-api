"""
CATCH Buzz Engine — 신호 수집 + 점수 계산
트렌드 목록은 Firestore catch_config/trends 에서 로드됩니다 (main.py 참조).
신호원: Naver DataLab · Google Trends · Reddit
"""
import os
import time
import logging
from datetime import date, timedelta
import requests

logger = logging.getLogger(__name__)


# ── 신호 수집 함수 ────────────────────────────────────────────────────────────

def get_google_signal(trend: dict) -> float:
    """Google Trends 7일 평균 관심도 (0-100). 실패 시 -1."""
    try:
        from pytrends.request import TrendReq
        kws = trend.get("google_kw", [])[:5]
        if not kws:
            return -1.0
        pt = TrendReq(hl="ko", tz=540, timeout=(10, 30), retries=2, backoff_factor=1.0)
        pt.build_payload(kws, cat=0, timeframe="now 7-d", geo="KR")
        df = pt.interest_over_time()
        if df.empty:
            return -1.0
        return float(df[kws].mean(axis=1).mean())
    except Exception as e:
        logger.warning(f"[Google Trends] {trend['id']}: {e}")
        return -1.0


def get_naver_signal(trend: dict, client_id: str, client_secret: str) -> float:
    """Naver DataLab 검색어 트렌드 7일 평균 (0-100). API 키 없으면 -1."""
    if not client_id or not client_secret:
        return -1.0
    naver_kw = trend.get("naver_kw", [])
    if not naver_kw:
        return -1.0
    try:
        today = date.today()
        body = {
            "startDate": (today - timedelta(days=7)).strftime("%Y-%m-%d"),
            "endDate": today.strftime("%Y-%m-%d"),
            "timeUnit": "date",
            "keywordGroups": [
                {"groupName": trend["id"], "keywords": naver_kw[:5]}
            ],
        }
        r = requests.post(
            "https://naverapihub.apigw.ntruss.com/search-trend/v1/search",
            json=body,
            headers={
                "X-NCP-APIGW-API-KEY-ID": client_id,
                "X-NCP-APIGW-API-KEY": client_secret,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return -1.0
        ratios = [item["ratio"] for item in results[0]["data"]]
        return float(sum(ratios) / len(ratios)) if ratios else -1.0
    except Exception as e:
        logger.warning(f"[Naver DataLab] {trend['id']}: {e}")
        return -1.0


def get_reddit_signal(trend: dict, client_id: str, client_secret: str) -> float:
    """Reddit 관련 서브레딧 7일 언급 수 → 0-100 환산. API 키 없으면 -1."""
    if not client_id or not client_secret:
        return -1.0
    try:
        import praw
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="catch-fashion-radar/1.0",
        )
        subreddits = "streetwear+femalefashionadvice+kpopfashion+korea"
        count = 0
        for term in trend.get("reddit_terms", [])[:2]:
            posts = reddit.subreddit(subreddits).search(term, time_filter="week", limit=25)
            count += sum(1 for _ in posts)
        return float(min(count * 2, 100))
    except Exception as e:
        logger.warning(f"[Reddit] {trend['id']}: {e}")
        return -1.0


# ── 정규화 + 점수 계산 ────────────────────────────────────────────────────────

def normalize_batch(scores: list) -> list:
    """배치 내 유효값 min-max 정규화 (0-100). -1은 중간값(50)으로 채움."""
    valid = [s for s in scores if s >= 0]
    if not valid:
        return [50.0] * len(scores)
    mn, mx = min(valid), max(valid)
    if mx == mn:
        return [50.0] * len(scores)
    return [
        50.0 if s < 0 else round((s - mn) / (mx - mn) * 100, 1)
        for s in scores
    ]


def compute_status(score: float, delta: float):
    if score >= 70 and -8 <= delta <= 8:
        return "hot", "● Hot · at peak"
    elif delta >= 20:
        return "rise", "▲ Rising fast"
    elif delta >= 8:
        return "rise", "▲ Rising"
    elif delta <= -8:
        return "cool", "▼ Cooling"
    else:
        return "steady", "→ Steady"


# ── 메인 업데이트 함수 ────────────────────────────────────────────────────────

def run_buzz_update(prev_data: dict, trends_config: list) -> list:
    """
    신호 수집 → 점수 계산 → 트렌드 리스트 반환.
    prev_data: {trend_id: {score, history}} (Firestore 전일 데이터)
    trends_config: Firestore catch_config/trends 에서 로드한 트렌드 목록
    """
    naver_id      = os.environ.get("NAVER_CLIENT_ID", "")
    naver_secret  = os.environ.get("NAVER_CLIENT_SECRET", "")
    reddit_id     = os.environ.get("REDDIT_CLIENT_ID", "")
    reddit_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")

    google_raw, naver_raw, reddit_raw = [], [], []

    for trend in trends_config:
        logger.info(f"Collecting: {trend['id']}")

        g = get_google_signal(trend)
        google_raw.append(g)
        time.sleep(2)

        n = get_naver_signal(trend, naver_id, naver_secret)
        naver_raw.append(n)

        r = get_reddit_signal(trend, reddit_id, reddit_secret)
        reddit_raw.append(r)

    g_norm = normalize_batch(google_raw)
    n_norm = normalize_batch(naver_raw)
    r_norm = normalize_batch(reddit_raw)

    has_naver  = any(s >= 0 for s in naver_raw)
    has_reddit = any(s >= 0 for s in reddit_raw)

    results = []
    for i, trend in enumerate(trends_config):
        if has_naver and has_reddit:
            raw = g_norm[i] * 0.45 + n_norm[i] * 0.30 + r_norm[i] * 0.25
        elif has_naver:
            raw = g_norm[i] * 0.60 + n_norm[i] * 0.40
        elif has_reddit:
            raw = g_norm[i] * 0.60 + r_norm[i] * 0.40
        else:
            raw = g_norm[i]

        score = round(raw)

        prev_score = prev_data.get(trend["id"], {}).get("score", score)
        delta = round(score - prev_score)
        status, status_label = compute_status(score, delta)

        prev_history = prev_data.get(trend["id"], {}).get("history", [])
        history = (prev_history[-6:] if prev_history else []) + [score]

        results.append({
            "id":           trend["id"],
            "name":         trend.get("name", trend["id"]),
            "description":  trend.get("description", ""),
            "see_it_kw":    trend.get("see_it_kw", trend.get("name", "") + " korean fashion"),
            "spotted_on":   trend.get("spotted_on"),
            "score":        score,
            "delta":        delta,
            "status":       status,
            "status_label": status_label,
            "history":      history,
            "signals": {
                "google": round(google_raw[i], 1),
                "naver":  round(naver_raw[i], 1),
                "reddit": round(reddit_raw[i], 1),
            },
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
        r["hero"] = (i == 0)

    return results
