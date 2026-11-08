"""
CATCH Buzz Engine — 신호 수집 + 점수 계산
트렌드 목록은 Firestore catch_config/trends 에서 로드됩니다 (main.py 참조).
신호원: Naver DataLab · Google Trends · Naver 블로그 글 수 · YouTube 영상 수
"""
import os
import re
import html
import math
import time
import logging
from datetime import date, datetime, timedelta
import requests

logger = logging.getLogger(__name__)

NAVER_MAX_GROUPS = 5
NAVER_WEEKS = 8
BLOG_URL = "https://naverapihub.apigw.ntruss.com/search/v1/blog"
BLOG_PAGE_SIZE = 100
BLOG_MAX_START = 1000
BLOG_FULL_SCALE = 1000  # 한 주 글 수가 이만큼이면 100점 (실제 분포를 보고 조정)
YOUTUBE_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_WEEKS = 2  # 호출 1회가 100유닛이라 변화량에 필요한 2주만 본다
YOUTUBE_PAGE_SIZE = 50
YOUTUBE_MAX_PAGES = 3
YOUTUBE_FULL_SCALE = 100  # 한 주 영상 수가 이만큼이면 100점 (실제 분포를 보고 조정)
# Google·네이버는 "검색" 한 묶음이라, 어느 한쪽이 없어도 검색 비중은 그대로다.
SEARCH_SPLIT = {"google": 0.60, "naver": 0.40}
WEIGHTS = {"search": 0.50, "blog": 0.30, "youtube": 0.20}
TAG_PATTERN = re.compile(r"<[^>]+>")
_DIAG = {}  # 임시 진단: /cron/update 응답으로만 내보낸다


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


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", html.unescape(TAG_PATTERN.sub("", text or ""))).lower()


def _weekly_counts(dates, end: date, weeks: int, read_until: date, full_scale: int) -> list:
    """
    날짜 목록을 주별 개수(오래된 주 → 최근 주)로 묶는다.
    read_until 이전이 포함되어 끝까지 못 읽은 주는 None이다. 단 이미 만점 기준을 넘긴 주는 값을 유지한다.
    """
    counts = [0] * weeks
    for day in dates:
        week = weeks - 1 - (end - day).days // 7
        if 0 <= week < weeks:
            counts[week] += 1
    return [
        count if count >= full_scale
        or end - timedelta(days=(weeks - 1 - week) * 7 + 6) >= read_until
        else None
        for week, count in enumerate(counts)
    ]


def _blog_posts(keyword: str, oldest: date, headers: dict) -> tuple:
    """
    keyword가 제목·요약에 실제로 들어간 글을 최신순으로 oldest까지 모은다.
    반환: ({링크: 작성일}, 글을 빠짐없이 읽은 가장 오래된 날짜). 끝까지 읽었으면 oldest.
    API는 1,000번째 글까지만 주므로, 그 전에 끊기면 마지막 날은 일부만 읽은 것으로 보고 뺀다.
    """
    posts, reached = {}, None
    target = _compact(keyword)
    stats = _DIAG.setdefault("blog", {}).setdefault(keyword, {"read": 0, "matched": 0, "reached": None, "sample": []})
    for start in range(1, BLOG_MAX_START + 1, BLOG_PAGE_SIZE):
        r = requests.get(
            BLOG_URL,
            params={"query": keyword, "display": BLOG_PAGE_SIZE, "start": start, "sort": "date"},
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        for item in items:
            stats["read"] += 1
            if len(stats["sample"]) < 3:
                stats["sample"].append(_compact(item.get("title"))[:40])
            try:
                posted = datetime.strptime(item.get("postdate", ""), "%Y%m%d").date()
            except ValueError:
                continue
            reached = posted
            stats["reached"] = posted.isoformat()
            if posted >= oldest and target in _compact(f"{item.get('title')} {item.get('description')}"):
                posts[item["link"]] = posted
                stats["matched"] += 1
        if len(items) < BLOG_PAGE_SIZE or (reached and reached < oldest):
            return posts, oldest
    return posts, reached + timedelta(days=1) if reached else date.max


def get_blog_weekly(trends: list, client_id: str, client_secret: str) -> list:
    """
    트렌드별 주간 블로그 글 수(오래된 주 → 최근 주). 수집에 실패하면 None.
    """
    results = [None] * len(trends)
    if not client_id or not client_secret:
        return results
    headers = {"X-NCP-APIGW-API-KEY-ID": client_id, "X-NCP-APIGW-API-KEY": client_secret}
    # 오늘은 아직 집계 중이라 어제까지만 본다 (DataLab 주 구분과 동일)
    end = date.today() - timedelta(days=1)
    oldest = end - timedelta(days=NAVER_WEEKS * 7 - 1)
    for i, trend in enumerate(trends):
        keywords = trend.get("naver_kw", [])[:5]
        if not keywords:
            continue
        try:
            posts, read_until = {}, oldest
            for keyword in keywords:
                found, reached = _blog_posts(keyword, oldest, headers)
                posts.update(found)
                read_until = max(read_until, reached)
        except (requests.RequestException, ValueError) as e:
            # 키를 남기지 않도록 URL 없이 기록한다
            status = getattr(getattr(e, "response", None), "status_code", type(e).__name__)
            logger.warning(f"[Naver 블로그] {trend['id']}: {status}")
            _DIAG.setdefault("errors", []).append(f"blog {trend['id']}: {status}")
            continue
        results[i] = _weekly_counts(posts.values(), end, NAVER_WEEKS, read_until, BLOG_FULL_SCALE)
    return results


def _youtube_dates(keywords: list, oldest: date, end: date, api_key: str) -> tuple:
    """
    최신순으로 받은 영상 중 제목·설명에 키워드가 들어간 것을 모은다.
    반환: ({영상 id: 게시일}, 빠짐없이 읽은 가장 오래된 날짜). 호출마다 100유닛이라 페이지 수를 제한한다.
    """
    targets = [_compact(k) for k in keywords]
    params = {
        "part": "snippet",
        "q": "|".join(keywords),
        "type": "video",
        "order": "date",
        "regionCode": "KR",
        "relevanceLanguage": "ko",
        "maxResults": YOUTUBE_PAGE_SIZE,
        "publishedAfter": f"{oldest.isoformat()}T00:00:00Z",
        "publishedBefore": f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z",
        "key": api_key,
    }
    videos, reached = {}, None
    stats = _DIAG.setdefault("youtube", {}).setdefault(
        params["q"], {"read": 0, "matched": 0, "pages": 0, "reached": None, "total": None, "sample": []}
    )
    for _ in range(YOUTUBE_MAX_PAGES):
        r = requests.get(YOUTUBE_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        stats["pages"] += 1
        stats["total"] = data.get("pageInfo", {}).get("totalResults")
        for item in data.get("items", []):
            snippet = item["snippet"]
            published = date.fromisoformat(snippet["publishedAt"][:10])
            reached = published
            stats["read"] += 1
            stats["reached"] = published.isoformat()
            if len(stats["sample"]) < 3:
                stats["sample"].append(_compact(snippet.get("title"))[:40])
            text = _compact(f"{snippet.get('title')} {snippet.get('description')}")
            if any(target in text for target in targets):
                videos[item["id"]["videoId"]] = published
                stats["matched"] += 1
        if not data.get("nextPageToken"):
            return videos, oldest
        params["pageToken"] = data["nextPageToken"]
    return videos, reached + timedelta(days=1) if reached else date.max


def get_youtube_weekly(trends: list, api_key: str) -> list:
    """
    트렌드별 주간 영상 수. 길이는 NAVER_WEEKS로 맞추고 보지 않은 과거 주는 None이다.
    수집에 실패하면 None. totalResults는 추정치라 영상을 직접 센다.
    """
    results = [None] * len(trends)
    if not api_key:
        return results
    end = date.today() - timedelta(days=1)
    oldest = end - timedelta(days=YOUTUBE_WEEKS * 7 - 1)
    for i, trend in enumerate(trends):
        keywords = trend.get("naver_kw", [])[:5]
        if not keywords:
            continue
        try:
            videos, read_until = _youtube_dates(keywords, oldest, end, api_key)
        except (requests.RequestException, ValueError, KeyError) as e:
            # 예외 문구에 키가 든 URL이 포함되므로 상태 코드만 남긴다
            status = getattr(getattr(e, "response", None), "status_code", type(e).__name__)
            logger.warning(f"[YouTube] {trend['id']}: {status}")
            _DIAG.setdefault("errors", []).append(f"youtube {trend['id']}: {status}")
            continue
        counts = _weekly_counts(videos.values(), end, YOUTUBE_WEEKS, read_until, YOUTUBE_FULL_SCALE)
        results[i] = [None] * (NAVER_WEEKS - YOUTUBE_WEEKS) + counts
    return results


def _log_score(count: int, full_scale: int) -> float:
    """개수를 고정 기준의 0-100으로 바꾼다. 차이가 커서 로그를 쓴다."""
    return min(100.0, 100 * math.log10(1 + count) / math.log10(1 + full_scale))


def blog_score(count: int) -> float:
    return _log_score(count, BLOG_FULL_SCALE)


def youtube_score(count: int) -> float:
    return _log_score(count, YOUTUBE_FULL_SCALE)


# ── 점수 계산 ────────────────────────────────────────────────────────

def compute_score(signals: dict):
    """수집된 신호만 가중 평균(가중치 재분배). 신호가 하나도 없으면 None."""
    groups = {k: v for k, v in signals.items() if k in WEIGHTS and v >= 0}
    search = {k: signals[k] for k in SEARCH_SPLIT if signals.get(k, -1.0) >= 0}
    if search:
        groups["search"] = sum(v * SEARCH_SPLIT[k] for k, v in search.items()) / sum(SEARCH_SPLIT[k] for k in search)
    if not groups:
        return None
    total = sum(WEIGHTS[k] for k in groups)
    return round(sum(v * WEIGHTS[k] for k, v in groups.items()) / total)


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
    _DIAG.clear()

    google_raw = []

    for trend in trends_config:
        logger.info(f"Collecting: {trend['id']}")

        google_raw.append(get_google_signal(trend))
        time.sleep(2)

    naver_weekly = get_naver_weekly(trends_config, naver_id, naver_secret)
    blog_weekly = get_blog_weekly(trends_config, naver_id, naver_secret)
    youtube_weekly = get_youtube_weekly(trends_config, os.environ.get("YOUTUBE_API_KEY", ""))

    results, insufficient = [], []
    for i, trend in enumerate(trends_config):
        weeks = naver_weekly[i]
        blog_counts = blog_weekly[i]
        blog_posts = blog_counts[-1] if blog_counts else None
        youtube_counts = youtube_weekly[i]
        youtube_videos = youtube_counts[-1] if youtube_counts else None
        search ={"google": google_raw[i], "naver": weeks[-1] if weeks else -1.0}
        # 이번 주 검색 신호가 없는 트렌드는 글 수만으로 피드에 올리지 않는다
        if compute_score(search) is None:
            insufficient.append(trend["id"])
            continue
        signals = {
            **search,
            "blog": blog_score(blog_posts) if blog_posts is not None else -1.0,
            "youtube": youtube_score(youtube_videos) if youtube_videos is not None else -1.0,
        }
        score = compute_score(signals)

        if weeks:
            # 네이버 값은 이전 문서와 비교할 수 없어 같은 응답의 주간 값으로 추이를 만든다.
            # 과거 값을 알 수 없는 신호는 이번 값으로 고정한다.
            blog_history = blog_counts or [None] * len(weeks)
            youtube_history = youtube_counts or [None] * len(weeks)
            history = [
                compute_score({
                    **signals,
                    "naver": week,
                    "blog": blog_score(posts) if posts is not None else signals["blog"],
                    "youtube": youtube_score(videos) if videos is not None else signals["youtube"],
                })
                for week, posts, videos in zip(weeks, blog_history, youtube_history)
            ]
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
            "blog_posts":   blog_posts,
            "youtube_videos": youtube_videos,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
        r["hero"] = (i == 0)

    return results, insufficient
