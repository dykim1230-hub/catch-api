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

from buzz_engine import run_buzz_update

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="CATCH Buzz API")

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

        trends = run_buzz_update(prev_data)

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


@app.get("/buzz/latest")
def get_latest():
    """오늘(없으면 어제) buzz 데이터 반환. 디버그·수동 확인용."""
    db = get_db()
    for d in [date.today().isoformat(), (date.today() - timedelta(days=1)).isoformat()]:
        doc = db.collection("catch_buzz").document(d).get()
        if doc.exists:
            return doc.to_dict()
    return {"error": "데이터 없음 — /cron/update를 먼저 실행하세요."}
