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

NAVER_MAX_GROUPS = 5
NAVER_PERIOD_DAYS = 8  # startDate~endDate 양 끝 포함
WEIGHTS = {"google": 0.45, "naver": 0.30, "reddit": 0.25}


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


def get_naver_signals(trends: list, client_id: str, client_secret: str) -> list:
    """
    Naver DataLab 7일 평균 (0-100). 실패·미수집 트렌드는 -1.
    DataLab 비율은 요청 안의 최댓값이 100인 상대값이라, 트렌드끼리 비교하려면
    한 요청에 함께 넣어야 한다. 한 요청 최대 5그룹이므로 초과분은 수집하지 않는다.
    """
    signals = [-1.0] * len(trends)
    if not client_id or not client_secret:
        return signals
    targets = [(i, t) for i, t in enumerate(trends) if t.get("naver_kw")]
    if len(targets) > NAVER_MAX_GROUPS:
        logger.warning(f"[Naver DataLab] 트렌드 {len(targets)}개 중 {NAVER_MAX_GROUPS}개만 수집")
        targets = targets[:NAVER_MAX_GROUPS]
    if not targets:
        return signals
    try:
        today = date.today()
        body = {
            "startDate": (today - timedelta(days=NAVER_PERIOD_DAYS - 1)).strftime("%Y-%m-%d"),
            "endDate": today.strftime("%Y-%m-%d"),
            "timeUnit": "date",
            "keywordGroups": [
                {"groupName": t["id"], "keywords": t["naver_kw"][:5]} for _, t in targets
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
        by_id = {item["title"]: item["data"] for item in r.json().get("results", [])}
        for i, t in targets:
            ratios = [point["ratio"] for point in by_id.get(t["id"], [])]
            if ratios:
                # 검색량이 없는 날은 응답에서 빠지므로 기간 전체 일수로 나눈다
                signals[i] = float(sum(ratios) / NAVER_PERIOD_DAYS)
    except Exception as e:
        logger.warning(f"[Naver DataLab] {e}")
    return signals


def get_reddit_signal(trend: dict, client_id: str = "", client_secret: str = "") -> float:
    """Reddit 공개 JSON API로 7일 언급 수 → 0-100 환산. 앱 등록 불필요."""
    # Gemini가 만든 기존 트렌드에는 reddit_terms가 없을 수 있어 google_kw로 대체
    terms = trend.get("reddit_terms") or trend.get("google_kw") or []
    if not terms:
        return -1.0
    try:
        headers = {"User-Agent": "catch-fashion-radar/1.0"}
        count = 0
        for term in terms[:2]:
            url = (
                "https://www.reddit.com/search.json"
                f"?q={requests.utils.quote(term)}&sort=new&t=week&limit=25"
            )
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            count += r.json().get("data", {}).get("dist", 0)
            time.sleep(1)
        return float(min(count * 2, 100))
    except Exception as e:
        logger.warning(f"[Reddit] {trend['id']}: {e}")
        return -1.0


# ── 점수 계산 ────────────────────────────────────────────────────────

def compute_score(signals: dict):
    """수집된 신호만 가중 평균(가중치 재분배). 신호가 하나도 없으면 None."""
    available = {k: v for k, v in signals.items() if v >= 0}
    if not available:
        return None
    total = sum(WEIGHTS[k] for k in available)
    return round(sum(v * WEIGHTS[k] for k, v in available.items()) / total)


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

def run_buzz_update(prev_data: dict, trends_config: list):
    """
    신호 수집 → 점수 계산 → (점수 있는 트렌드 리스트, 신호 없는 트렌드 id 리스트) 반환.
    prev_data: {trend_id: {score, history}} (Firestore 직전 데이터)
    trends_config: Firestore catch_config/trends 에서 로드한 트렌드 목록
    """
    naver_id      = os.environ.get("NAVER_CLIENT_ID", "")
    naver_secret  = os.environ.get("NAVER_CLIENT_SECRET", "")

    google_raw, reddit_raw = [], []

    for trend in trends_config:
        logger.info(f"Collecting: {trend['id']}")

        google_raw.append(get_google_signal(trend))
        time.sleep(2)

        reddit_raw.append(get_reddit_signal(trend))

    naver_raw = get_naver_signals(trends_config, naver_id, naver_secret)

    results, insufficient = [], []
    for i, trend in enumerate(trends_config):
        signals = {"google": google_raw[i], "naver": naver_raw[i], "reddit": reddit_raw[i]}
        score = compute_score(signals)
        if score is None:
            insufficient.append(trend["id"])
            continue

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
            "signals": {k: round(v, 1) for k, v in signals.items()},
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
        r["hero"] = (i == 0)

    return results, insufficient
