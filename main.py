"""
CATCH Buzz API — FastAPI on Render
엔드포인트:
  GET  /              헬스 체크
  POST /cron/update   신호 수집 + Firestore 저장 (cron-job.org 호출)
  GET  /buzz/latest   최신 buzz 데이터 반환 (디버그용)
"""
import os
import json
import base64
import logging
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, Request
import firebase_admin
from firebase_admin import credentials, firestore as fs
import google.genai as google_genai

from buzz_engine import run_buzz_update

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="CATCH Buzz API")

# ── 기본 트렌드 목록 (Firestore에 데이터 없을 때 폴백) ───────────────────────
DEFAULT_TRENDS = [
    {
        "id": "granola_core",
        "name": "Granola-core",
        "description": "Vests, straps, and warm earthy colors — like camping clothes, but for the city.",
        "see_it_kw": "granola core fashion korea",
        "spotted_on": "a boy-group idol",
        "google_kw": ["granola core fashion", "gorpcore"],
        "naver_kw": ["그래놀라코어", "고프코어"],
        "reddit_terms": ["gorpcore", "granola core"],
    },
    {
        "id": "oversized_totes",
        "name": "Oversized Totes",
        "description": "Big square bags carried on the shoulder — Vogue's pick for 2026, now all over Seoul.",
        "see_it_kw": "oversized tote bag outfit korea",
        "spotted_on": "a Seoul Fashion Week guest",
        "google_kw": ["oversized tote bag", "tote bag korea"],
        "naver_kw": ["오버사이즈 토트백", "빅 토트백"],
        "reddit_terms": ["oversized tote bag", "big tote"],
    },
    {
        "id": "open_back_tops",
        "name": "Open-Back Tops",
        "description": "A top with an open back worn over a simple layer — fashion magazines call it one of the hottest trends.",
        "see_it_kw": "open back top outfit korea",
        "spotted_on": "a girl-group idol",
        "google_kw": ["open back top fashion", "backless top korean"],
        "naver_kw": ["오픈백 탑", "백리스"],
        "reddit_terms": ["open back top outfit", "backless top"],
    },
    {
        "id": "color_liberation",
        "name": "Color Liberation",
        "description": "Bright colors are back — bold blue, pink, and yellow instead of just black and beige.",
        "see_it_kw": "korean bold color street style 2026",
        "spotted_on": "a drama lead",
        "google_kw": ["bold color fashion korea", "color blocking street style"],
        "naver_kw": ["컬러 패션", "비비드 컬러"],
        "reddit_terms": ["bold colors fashion", "color blocking outfit"],
    },
]

# ── Firebase 초기화 ───────────────────────────────────────────────────────────
_db = None

def get_db():
    global _db
    if _db is None:
        if not firebase_admin._apps:
            sa_b64 = os.environ.get("FIREBASE_SA_JSON_B64")
            if not sa_b64:
                raise RuntimeError("FIREBASE_SA_JSON_B64 환경 변수가 설정되지 않았습니다.")
            sa_dict = json.loads(base64.b64decode(sa_b64))
            cred = credentials.Certificate(sa_dict)
            firebase_admin.initialize_app(cred)
        _db = fs.client()
    return _db


def load_trends_config(db) -> list:
    """Firestore catch_config/trends 에서 로드. 없으면 DEFAULT_TRENDS 반환."""
    try:
        doc = db.collection("catch_config").document("trends").get()
        if doc.exists:
            trends = doc.to_dict().get("trends", [])
            if trends:
                logger.info(f"Firestore 트렌드 {len(trends)}개 로드")
                return trends
    except Exception as e:
        logger.warning(f"catch_config/trends 로드 실패: {e}")
    logger.info("DEFAULT_TRENDS 사용")
    return DEFAULT_TRENDS


# ── 엔드포인트 ────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "service": "catch-buzz-api"}


@app.post("/cron/update")
async def cron_update(request: Request):
    """
    cron-job.org에서 매일 08:00 KST에 호출.
    신호 수집 → buzz 점수 계산 → Firestore `catch_buzz/{YYYY-MM-DD}` 저장.
    """
    import traceback
    try:
        # 보안 토큰 확인
        token    = request.headers.get("X-Cron-Token", "")
        expected = os.environ.get("CRON_SECRET", "")
        if expected and token != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

        db = get_db()
        today     = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        # 전날 데이터 로드 (delta + history 누적용)
        prev_doc  = db.collection("catch_buzz").document(yesterday).get()
        prev_data = {}
        if prev_doc.exists:
            for t in prev_doc.to_dict().get("trends", []):
                prev_data[t["id"]] = {"score": t["score"], "history": t.get("history", [])}

        logger.info(f"Buzz update 시작: {today} | prev_ids: {list(prev_data.keys())}")

        trends_config = load_trends_config(db)
        trends = run_buzz_update(prev_data, trends_config)

        db.collection("catch_buzz").document(today).set({
            "date": today,
            "updated_at": fs.SERVER_TIMESTAMP,
            "trends": trends,
        })

        logger.info(f"Buzz update 완료: { {t['id']: t['score'] for t in trends} }")
        return {"status": "ok", "date": today, "trends": trends}
    except HTTPException:
        raise
    except Exception as e:
        err = traceback.format_exc()
        logger.error(f"cron_update 오류:\n{err}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")



@app.post("/cron/refresh-trends")
async def refresh_trends(request: Request):
    """
    Gemini로 K-패션 트렌드 5개 자동 생성 → Firestore catch_config/trends 저장.
    주 1회 호출 권장 (매주 월요일 등).
    """
    import traceback
    try:
        token    = request.headers.get("X-Cron-Token", "")
        expected = os.environ.get("CRON_SECRET", "")
        if expected and token != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

        today = date.today()
        month = today.month
        season = "Spring" if month in [3,4,5] else "Summer" if month in [6,7,8] else "Autumn" if month in [9,10,11] else "Winter"

        prompt = f"""Today is {today.isoformat()} (Seoul, Korea). Season: {season}.
You are a K-fashion trend curator. Generate exactly 5 K-fashion trends currently popular in Seoul street style.
Consider the current season and recent runway and street style trends.

Return ONLY a JSON array, no explanation, no markdown code blocks:
[
  {{
    "id": "snake_case_id",
    "name": "Trend Name in English",
    "description": "One casual English sentence (max 25 words) — what the look is and why it is popular.",
    "naver_kw": ["한국어키워드1", "한국어키워드2"],
    "google_kw": ["english keyword 1", "english keyword 2"],
    "see_it_kw": "search term for Google Images",
    "spotted_on": "short description like a girl-group idol or a drama lead or null"
  }}
]"""

        client = google_genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        new_trends = json.loads(raw)
        if not isinstance(new_trends, list) or len(new_trends) == 0:
            raise ValueError("Gemini가 올바른 배열을 반환하지 않음")

        db = get_db()
        db.collection("catch_config").document("trends").set({
            "updated_at":   fs.SERVER_TIMESTAMP,
            "updated_date": today.isoformat(),
            "trends":       new_trends,
        })

        logger.info(f"트렌드 갱신 완료: {[t['name'] for t in new_trends]}")
        return {"status": "ok", "updated_date": today.isoformat(), "trends": new_trends}

    except HTTPException:
        raise
    except Exception as e:
        err = traceback.format_exc()
        logger.error(f"refresh_trends 오류:\n{err}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/buzz/latest")
def get_latest():
    """오늘(없으면 어제) buzz 데이터 반환. 디버그·수동 확인용."""
    db = get_db()
    for d in [date.today().isoformat(), (date.today() - timedelta(days=1)).isoformat()]:
        doc = db.collection("catch_buzz").document(d).get()
        if doc.exists:
            return doc.to_dict()
    return {"error": "데이터 없음 — /cron/update를 먼저 실행하세요."}
