"""
CATCH Buzz Engine — 신호 수집 + 점수 계산
신호원: Google Trends (45%) · Naver DataLab (30%) · Reddit (25%)
"""
import os
import time
import logging
from datetime import date, timedelta
import requests

logger = logging.getLogger(__name__)

# ── 트렌드 키워드 정의 ────────────────────────────────────────────────────────
# id는 catch-web/index.html의 TRENDS 배열 id와 반드시 일치해야 합니다.
TRENDS_CONFIG = [
    {
        "id": "granola_core",
        "name": "Granola-core",
        "google_kw": ["granola core fashion", "gorpcore"],
        "naver_kw": ["그래놀라코어", "고프코어"],
        "reddit_terms": ["gorpcore", "granola core"],
    },
    {
        "id": "oversized_totes",
        "name": "Oversized Totes",
        "google_kw": ["oversized tote bag", "tote bag korea"],
        "naver_kw": ["오버사이즈 토트백", "빅 토트백"],
        "reddit_terms": ["oversized tote bag", "big tote"],
    },
    {
        "id": "open_back_tops",
        "name": "Open-Back Tops",
        "google_kw": ["open back top fashion", "backless top korean"],
        "naver_kw": ["오픈백 탑", "백리스"],
        "reddit_terms": ["open back top outfit", "backless top"],
    },
    {
        "id": "color_liberation",
        "name": "Color Liberation",
        "google_kw": ["bold color fashion korea", "color blocking street style"],
        "naver_kw": ["컬러 패션", "비비드 컬러"],
        "reddit_terms": ["bold colors fashion", "color blocking outfit"],
    },
]


# ── 신호 수집 함수 ────────────────────────────────────────────────────────────

def get_google_signal(trend: dict) -> float:
    """Google Trends 7일 평균 관심도 (0-100). 실패 시 -1."""
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="ko", tz=540, timeout=(10, 30), retries=2, backoff_factor=1.0)
        kws = trend["google_kw"][:5]
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
    try:
        today = date.today()
        body = {
            "startDate": (today - timedelta(days=7)).strftime("%Y-%m-%d"),
            "endDate": today.strftime("%Y-%m-%d"),
            "timeUnit": "date",
            "keywordGroups": [
                {"groupName": trend["id"], "keywords": trend["naver_kw"][:5]}
            ],
        }
        r = requests.post(
            "https://openapi.naver.com/v1/datalab/search",
            json=body,
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
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
        for term in trend["reddit_terms"][:2]:
            posts = reddit.subreddit(subreddits).search(term, time_filter="week", limit=25)
            count += sum(1 for _ in posts)
        # 최대 50개 → 100점 환산
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

def run_buzz_update(prev_data: dict) -> list:
    """
    신호 수집 → 점수 계산 → 트렌드 리스트 반환.
    prev_data: {trend_id: {score, history}} (Firestore 전일 데이터)
    """
    naver_id     = os.environ.get("NAVER_CLIENT_ID", "")
    naver_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    reddit_id    = os.environ.get("REDDIT_CLIENT_ID", "")
    reddit_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")

    google_raw, naver_raw, reddit_raw = [], [], []

    for trend in TRENDS_CONFIG:
        logger.info(f"Collecting: {trend['id']}")

        g = get_google_signal(trend)
        google_raw.append(g)
        time.sleep(2)  # Google rate limit 방지

        n = get_naver_signal(trend, naver_id, naver_secret)
        naver_raw.append(n)

        r = get_reddit_signal(trend, reddit_id, reddit_secret)
        reddit_raw.append(r)

    # 각 신호 배치 정규화
    g_norm = normalize_batch(google_raw)
    n_norm = normalize_batch(naver_raw)
    r_norm = normalize_batch(reddit_raw)

    # 사용 가능한 소스 기반 가중치 재조정
    has_naver  = any(s >= 0 for s in naver_raw)
    has_reddit = any(s >= 0 for s in reddit_raw)

    results = []
    for i, trend in enumerate(TRENDS_CONFIG):
        if has_naver and has_reddit:
            raw = g_norm[i] * 0.45 + n_norm[i] * 0.30 + r_norm[i] * 0.25
        elif has_naver:
            raw = g_norm[i] * 0.60 + n_norm[i] * 0.40
        elif has_reddit:
            raw = g_norm[i] * 0.60 + r_norm[i] * 0.40
        else:
            raw = g_norm[i]

        score = round(raw)

        # delta 계산 (전일 점수 기준)
        prev_score = prev_data.get(trend["id"], {}).get("score", score)
        delta = round(score - prev_score)
        status, status_label = compute_status(score, delta)

        # 7일 히스토리 누적 (스파크 차트용)
        prev_history = prev_data.get(trend["id"], {}).get("history", [])
        history = (prev_history[-6:] if prev_history else []) + [score]

        results.append({
            "id": trend["id"],
            "score": score,
            "delta": delta,
            "status": status,
            "status_label": status_label,
            "history": history,
            "signals": {
                "google": round(google_raw[i], 1),
                "naver": round(naver_raw[i], 1),
                "reddit": round(reddit_raw[i], 1),
            },
        })

    # 점수 내림차순 정렬 → rank, hero 부여
    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
        r["hero"] = (i == 0)

    return results
