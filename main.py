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
from trend_picker import normalize_name, pick_trends

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
    },
    {
        "id": "oversized_totes",
        "name": "Oversized Totes",
        "description": "Big square bags carried on the shoulder — Vogue's pick for 2026, now all over Seoul.",
        "see_it_kw": "oversized tote bag outfit korea",
        "spotted_on": "a Seoul Fashion Week guest",
        "google_kw": ["oversized tote bag", "tote bag korea"],
        "naver_kw": ["오버사이즈 토트백", "빅 토트백"],
    },
    {
        "id": "open_back_tops",
        "name": "Open-Back Tops",
        "description": "A top with an open back worn over a simple layer — fashion magazines call it one of the hottest trends.",
        "see_it_kw": "open back top outfit korea",
        "spotted_on": "a girl-group idol",
        "google_kw": ["open back top fashion", "backless top korean"],
        "naver_kw": ["오픈백 탑", "백리스"],
    },
    {
        "id": "color_liberation",
        "name": "Color Liberation",
        "description": "Bright colors are back — bold blue, pink, and yellow instead of just black and beige.",
        "see_it_kw": "korean bold color street style 2026",
        "spotted_on": "a drama lead",
        "google_kw": ["bold color fashion korea", "color blocking street style"],
        "naver_kw": ["컬러 패션", "비비드 컬러"],
    },
]

# ── Firebase 초기화 ───────────────────────────────────────────────────────────
_db = None
PREV_MAX_AGE_DAYS = 14
TREND_EXCLUDE_DAYS = 56
TREND_HISTORY_DOCS = 30  # 수동 실행으로 문서가 늘어도 제외 기간을 덮을 만큼

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


def load_prev_data(db, today: date) -> dict:
    """
    오늘 이전의 가장 최근 문서를 변화량 기준으로 읽는다.
    PREV_MAX_AGE_DAYS보다 오래됐으면 "this week" 변화량이 될 수 없어 기준 없이 시작.
    """
    docs = (
        db.collection("catch_buzz")
        .order_by("__name__", direction="DESCENDING")
        .limit(2)
        .stream()
    )
    for doc in docs:
        if doc.id >= today.isoformat():
            continue
        if (today - date.fromisoformat(doc.id)).days > PREV_MAX_AGE_DAYS:
            return {}
        return {
            t["id"]: {"score": t["score"], "history": t.get("history", [])}
            for t in doc.to_dict().get("trends", [])
        }
    return {}


def load_recent_trends(db, today: date) -> tuple:
    """현재 목록과 최근 TREND_EXCLUDE_DAYS일 안에 쓴 트렌드의 (id 집합, 이름 목록)을 반환한다."""
    ids, names = set(), set()

    def add(trend):
        ids.add(trend["id"])
        if trend.get("name"):
            names.add(trend["name"])

    config = db.collection("catch_config").document("trends").get()
    if config.exists:
        for trend in config.to_dict().get("trends", []):
            add(trend)

    docs = (
        db.collection("catch_buzz")
        .order_by("__name__", direction="DESCENDING")
        .limit(TREND_HISTORY_DOCS)
        .stream()
    )
    cutoff = (today - timedelta(days=TREND_EXCLUDE_DAYS)).isoformat()
    for doc in docs:
        if doc.id < cutoff:
            break
        data = doc.to_dict()
        for trend in data.get("trends", []):
            add(trend)
        ids.update(data.get("insufficient_data", []))
    return ids, names


# ── 엔드포인트 ────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "service": "catch-buzz-api"}


@app.post("/cron/update")
async def cron_update(request: Request):
    """
    GitHub Actions에서 주 1회 호출 (.github/workflows/buzz-update.yml).
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
        today = date.today().isoformat()

        # 직전 데이터 로드 (delta + history 누적용)
        prev_data = load_prev_data(db, date.today())

        logger.info(f"Buzz update 시작: {today} | prev_ids: {list(prev_data.keys())}")

        trends_config = load_trends_config(db)
        trends, insufficient = run_buzz_update(prev_data, trends_config)

        # 전부 수집 실패면 저장하지 않아 사이트가 직전 데이터를 유지하게 한다
        if not trends:
            raise RuntimeError(f"모든 트렌드 신호 수집 실패: {insufficient}")

        db.collection("catch_buzz").document(today).set({
            "date": today,
            "updated_at": fs.SERVER_TIMESTAMP,
            "trends": trends,
            "insufficient_data": insufficient,
        })

        logger.info(f"Buzz update 완료: { {t['id']: t['score'] for t in trends} }")
        return {"status": "ok", "date": today, "trends": trends, "insufficient_data": insufficient}
    except HTTPException:
        raise
    except Exception as e:
        err = traceback.format_exc()
        logger.error(f"cron_update 오류:\n{err}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")



@app.post("/cron/refresh-trends")
async def refresh_trends(request: Request, dry_run: bool = False):
    """
    Gemini와 Google 검색으로 최근에 쓰지 않은 K-패션 트렌드를 뽑아 catch_config/trends 에 저장한다.
    dry_run=true 면 저장하지 않고 결과만 반환한다.
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
        db = get_db()
        excluded_ids, excluded_names = load_recent_trends(db, today)

        new_trends, sources = pick_trends(
            google_genai.Client(api_key=api_key),
            today,
            excluded_ids,
            {normalize_name(name) for name in excluded_names},
            os.environ.get("NAVER_CLIENT_ID", ""),
            os.environ.get("NAVER_CLIENT_SECRET", ""),
        )

        if not dry_run:
            db.collection("catch_config").document("trends").set({
                "updated_at":   fs.SERVER_TIMESTAMP,
                "updated_date": today.isoformat(),
                "trends":       new_trends,
                "sources":      sources,
            })

        logger.info(f"트렌드 갱신 완료 (dry_run={dry_run}): {[t['name'] for t in new_trends]}")
        return {
            "status": "ok",
            "dry_run": dry_run,
            "updated_date": today.isoformat(),
            "excluded_ids": sorted(excluded_ids),
            "trends": new_trends,
            "sources": sources,
        }

    except HTTPException:
        raise
    except Exception as e:
        err = traceback.format_exc()
        logger.error(f"refresh_trends 오류:\n{err}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/debug/gemini")
async def debug_gemini(request: Request):
    """임시 진단용: 모델과 검색 조합별 Gemini 호출 결과만 반환한다. 저장하지 않는다."""
    expected = os.environ.get("CRON_SECRET", "")
    if not expected or request.headers.get("X-Cron-Token", "") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    client = google_genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
    try:
        models = sorted(m.name for m in client.models.list() if "flash" in m.name)
    except Exception as e:
        models = [f"{type(e).__name__}: {e}"[:300]]

    results = []
    for model, search in [
        ("gemini-3.6-flash", False),
        ("gemini-3.6-flash", True),
        ("gemini-2.5-flash", False),
        ("gemini-2.5-flash", True),
    ]:
        config = google_genai.types.GenerateContentConfig(
            tools=[google_genai.types.Tool(google_search=google_genai.types.GoogleSearch())] if search else None,
        )
        try:
            client.models.generate_content(model=model, contents="Reply with OK.", config=config)
            results.append({"model": model, "search": search, "ok": True})
        except Exception as e:
            results.append({"model": model, "search": search, "ok": False, "error": f"{type(e).__name__}: {e}"[:600]})
    return {"flash_models": models, "results": results}


@app.get("/buzz/latest")
def get_latest():
    """가장 최근 buzz 데이터 반환. 디버그·수동 확인용."""
    db = get_db()
    docs = (
        db.collection("catch_buzz")
        .order_by("__name__", direction="DESCENDING")
        .limit(1)
        .stream()
    )
    for doc in docs:
        return doc.to_dict()
    return {"error": "데이터 없음 — /cron/update를 먼저 실행하세요."}
