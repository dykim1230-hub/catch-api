"""
Gemini + Google 검색으로 새 트렌드를 뽑고 검증한다.
실제로 확인되지 않은 트렌드와 연예인 이름이 피드에 오르지 않게 하는 것이 목적이다.
"""
import json
import logging
import re

from google.genai import types

from buzz_engine import get_naver_weekly

logger = logging.getLogger(__name__)

MODEL = "gemini-3.6-flash"
MIN_TRENDS = 3
MAX_ATTEMPTS = 2
ID_PATTERN = re.compile(r"^[a-z0-9_]+$")


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def build_prompt(today, excluded_names: list) -> str:
    month = today.month
    season = "Spring" if month in [3, 4, 5] else "Summer" if month in [6, 7, 8] else "Autumn" if month in [9, 10, 11] else "Winter"
    excluded = ", ".join(sorted(excluded_names)) or "none"
    return f"""Today is {today.isoformat()} (Seoul, Korea). Season: {season}.
You are a K-fashion trend curator. Search the web, including Korean-language sources from the last 30 days,
and find exactly 5 K-fashion trends currently popular in Seoul street style.
Only include a trend when the search results support it.

Do not include these recently used trends or close variations of them: {excluded}

Write descriptions in your own words. Do not copy sentences from sources.
Set "spotted_on" to a real person's name only when a search result explicitly says that person wore this look.
Otherwise set it to null. Never guess.

Return ONLY a JSON array, no explanation, no markdown code blocks:
[
  {{
    "id": "snake_case_id",
    "name": "Trend Name in English",
    "description": "One casual English sentence (max 25 words) — what the look is and why it is popular.",
    "naver_kw": ["한국어키워드1", "한국어키워드2"],
    "google_kw": ["english keyword 1", "english keyword 2"],
    "see_it_kw": "search term for Google Images",
    "spotted_on": null
  }}
]"""


def parse_trends(raw: str) -> list:
    """응답에 설명이나 코드 블록이 섞여 있어도 JSON 배열만 읽는다."""
    start, end = (raw or "").find("["), (raw or "").rfind("]")
    if start < 0 or end <= start:
        raise ValueError("Gemini 응답에 JSON 배열이 없음")
    trends = json.loads(raw[start:end + 1])
    if not isinstance(trends, list):
        raise ValueError("Gemini가 배열을 반환하지 않음")
    return trends


def read_grounding(response) -> tuple:
    """(검색 출처 목록, 출처로 뒷받침된 응답 문장 목록)을 반환한다."""
    candidates = response.candidates or []
    metadata = candidates[0].grounding_metadata if candidates else None
    if metadata is None:
        return [], []
    sources = []
    for chunk in metadata.grounding_chunks or []:
        if chunk.web and chunk.web.uri:
            sources.append({"title": chunk.web.title or "", "url": chunk.web.uri})
    supported = [
        support.segment.text
        for support in metadata.grounding_supports or []
        if support.segment and support.segment.text and support.grounding_chunk_indices
    ]
    return sources, supported


def clean_trends(candidates: list, excluded_ids: set, excluded_names: set, supported: list) -> list:
    """형식이 맞고 최근에 쓰지 않은 트렌드만 남기고, 근거 없는 연예인 이름은 지운다."""
    cleaned, seen_ids, seen_names = [], set(), set()
    for trend in candidates:
        if not isinstance(trend, dict):
            continue
        trend_id, name = trend.get("id"), trend.get("name")
        if not isinstance(trend_id, str) or not ID_PATTERN.match(trend_id):
            continue
        if not isinstance(name, str) or not trend.get("description") or not trend.get("see_it_kw"):
            continue
        naver_kw = trend.get("naver_kw")
        if not isinstance(naver_kw, list) or not naver_kw:
            continue
        normalized = normalize_name(name)
        if trend_id in excluded_ids or trend_id in seen_ids:
            continue
        if normalized in excluded_names or normalized in seen_names:
            continue
        seen_ids.add(trend_id)
        seen_names.add(normalized)

        spotted = trend.get("spotted_on")
        has_source = isinstance(spotted, str) and any(spotted.lower() in text.lower() for text in supported)
        cleaned.append({**trend, "spotted_on": spotted if has_source else None})
    return cleaned


def pick_trends(client, today, excluded_ids: set, excluded_names: set, naver_id: str, naver_secret: str) -> tuple:
    """
    새 트렌드 (목록, 검색 출처)를 반환한다.
    MAX_ATTEMPTS번 안에 네이버 신호가 있는 트렌드를 MIN_TRENDS개 채우지 못하면 예외를 던진다.
    """
    excluded_ids, excluded_names = set(excluded_ids), set(excluded_names)
    prompt_names = list(excluded_names)
    failure = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=build_prompt(today, prompt_names),
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            sources, supported = read_grounding(response)
            if not sources:
                raise ValueError("검색 출처 없이 생성됨")
            cleaned = clean_trends(parse_trends(response.text), excluded_ids, excluded_names, supported)
        except ValueError as e:
            failure = str(e)
            logger.warning(f"트렌드 생성 {attempt}회차 실패: {failure}")
            continue

        weekly = get_naver_weekly(cleaned, naver_id, naver_secret)
        with_signal = [t for t, weeks in zip(cleaned, weekly) if weeks]
        if len(with_signal) >= MIN_TRENDS:
            return with_signal, sources

        failure = f"네이버 신호가 있는 트렌드 {len(with_signal)}개"
        logger.warning(f"트렌드 생성 {attempt}회차 부족: {failure}")
        # 검색량이 없던 트렌드를 다시 받지 않도록 다음 회차에서 제외한다
        for trend, weeks in zip(cleaned, weekly):
            if not weeks:
                excluded_ids.add(trend["id"])
                excluded_names.add(normalize_name(trend["name"]))
                prompt_names.append(trend["name"])
    raise RuntimeError(f"새 트렌드 생성 실패: {failure}")
