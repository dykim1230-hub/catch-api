"""
CATCH Buzz Engine — 신호 수집 + 점수 계산
트렌드 목록은 Firestore catch_config/trends 에서 로드됩니다 (main.py 참조).
신호원: Naver DataLab · Google Trends
"""
import os
import time
import logging
from datetime import date, timedelta
import requests

logger = logging.getLogger(__name__)

NAVER_MAX_GROUPS = 5
NAVER_WEEKS = 8
WEIGHTS = {"google": 0.60, "naver": 0.40}


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


def get_naver_weekly(trends: list, client_id: str, client_secret: str) -> list:
    """
    Naver DataLab 주간 평균 목록(오래된 주 → 최근 주, 0-100). 이번 주 데이터가 없으면 None.
    DataLab 비율은 요청 안의 최댓값이 100인 상대값이라, 다른 요청의 값과 비교할 수 없다.
    그래서 트렌드들과 여러 주를 한 요청에 함께 넣어 같은 기준의 값을 받는다.
    한 요청 최대 5그룹이므로 초과분은 수집하지 않는다.
    """
    weekly = [None] * len(trends)
    if not client_id or not client_secret:
        return weekly
    targets = [(i, t) for i, t in enumerate(trends) if t.get("naver_kw")]
    if len(targets) > NAVER_MAX_GROUPS:
        logger.warning(f"[Naver DataLab] 트렌드 {len(targets)}개 중 {NAVER_MAX_GROUPS}개만 수집")
        targets = targets[:NAVER_MAX_GROUPS]
    if not targets:
        return weekly
    try:
        # 오늘은 아직 집계 중이라 어제까지만 본다
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=NAVER_WEEKS * 7 - 1)
        body = {
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
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
            totals = [0.0] * NAVER_WEEKS
            has_this_week = False
            for point in by_id.get(t["id"], []):
                days_ago = (end - date.fromisoformat(point["period"])).days
                week = NAVER_WEEKS - 1 - days_ago // 7
                if 0 <= week < NAVER_WEEKS:
                    totals[week] += point["ratio"]
                    has_this_week = has_this_week or week == NAVER_WEEKS - 1
            if has_this_week:
                # 검색량이 없는 날은 응답에서 빠지므로 7일로 나눈다
                weekly[i] = [total / 7 for total in totals]
    except Exception as e:
        logger.warning(f"[Naver DataLab] {e}")
    return weekly


def _naver_sums(groups: list, start: date, end: date, client_id: str, client_secret: str) -> dict:
    """한 요청(최대 5그룹)의 그룹별 비율 합계. 응답에 없는 그룹은 0."""
    r = requests.post(
        "https://naverapihub.apigw.ntruss.com/search-trend/v1/search",
        json={
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
            "timeUnit": "date",
            "keywordGroups": [{"groupName": t["id"], "keywords": t["naver_kw"][:5]} for t in groups],
        },
        headers={
            "X-NCP-APIGW-API-KEY-ID": client_id,
            "X-NCP-APIGW-API-KEY": client_secret,
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    r.raise_for_status()
    sums = {t["id"]: 0.0 for t in groups}
    for item in r.json().get("results", []):
        sums[item["title"]] = sum(point["ratio"] for point in item.get("data", []))
    return sums


def get_naver_this_week(trends: list, client_id: str, client_secret: str) -> list:
    """
    최근 7일 검색량을 트렌드끼리 비교할 수 있는 값으로 반환한다(같은 순서, 없으면 0).
    한 요청에 5그룹까지만 넣을 수 있어 5개가 넘으면 여러 번 요청한다.
    DataLab 값은 요청마다 기준이 달라서, 첫 요청에서 검색량이 가장 많은 트렌드를
    기준으로 삼아 나머지 요청에 함께 넣고 그 값의 비율로 맞춘다.
    """
    targets = [t for t in trends if t.get("naver_kw")]
    if not client_id or not client_secret or not targets:
        return [0.0] * len(trends)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=6)
    first = targets[:NAVER_MAX_GROUPS]
    values = _naver_sums(first, start, end, client_id, client_secret)
    anchor = max(first, key=lambda t: values[t["id"]])
    anchor_value = values[anchor["id"]]
    rest = targets[NAVER_MAX_GROUPS:]
    if rest and anchor_value <= 0:
        raise RuntimeError("기준 트렌드의 검색량이 0이라 비교할 수 없음")
    for k in range(0, len(rest), NAVER_MAX_GROUPS - 1):
        chunk = rest[k:k + NAVER_MAX_GROUPS - 1]
        sums = _naver_sums([anchor] + chunk, start, end, client_id, client_secret)
        if sums[anchor["id"]] <= 0:
            raise RuntimeError("기준 트렌드의 검색량이 0이라 비교할 수 없음")
        scale = anchor_value / sums[anchor["id"]]
        for t in chunk:
            values[t["id"]] = sums[t["id"]] * scale
    return [values.get(t["id"], 0.0) for t in trends]


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

    google_raw = []

    for trend in trends_config:
        logger.info(f"Collecting: {trend['id']}")

        google_raw.append(get_google_signal(trend))
        time.sleep(2)

    naver_weekly = get_naver_weekly(trends_config, naver_id, naver_secret)

    results, insufficient = [], []
    for i, trend in enumerate(trends_config):
        weeks = naver_weekly[i]
        signals = {"google": google_raw[i], "naver": weeks[-1] if weeks else -1.0}
        score = compute_score(signals)
        if score is None:
            insufficient.append(trend["id"])
            continue

        if weeks:
            # 네이버 값은 이전 문서와 비교할 수 없어 같은 응답의 주간 값으로 추이를 만든다.
            # 과거 주의 다른 신호는 알 수 없어 이번 값으로 고정한다.
            history = [compute_score({**signals, "naver": week}) for week in weeks]
            delta = history[-1] - history[-2]
        else:
            prev_score = prev_data.get(trend["id"], {}).get("score", score)
            delta = round(score - prev_score)
            prev_history = prev_data.get(trend["id"], {}).get("history", [])
            history = (prev_history[-6:] if prev_history else []) + [score]

        status, status_label = compute_status(score, delta)

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
