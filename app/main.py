import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config, interviewer, session as store, stt
from app.schools import (
    DEFAULT_ANSWER_LIMIT_SEC,
    MAX_QUESTIONS,
    MIN_REMAINING_SEC,
    SCHOOLS,
    get_school,
)

ANSWER_LIMIT_MIN_SEC = 20
ANSWER_LIMIT_MAX_SEC = 300
# 질문 하나를 읽어 주는 데 걸리는 대략의 시간
QUESTION_READ_SEC = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

MAX_AUDIO_BYTES = 25 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    stt.load_in_background()
    yield


app = FastAPI(title="학종 모의면접", lifespan=lifespan)


@app.exception_handler(stt.STTError)
def stt_error(_, exc: stt.STTError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=500)


@app.exception_handler(interviewer.LLMError)
def llm_error(_, exc: interviewer.LLMError) -> JSONResponse:
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(store.RecordError)
def record_error(_, exc: store.RecordError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(store.SessionNotFound)
def session_not_found(_, exc: store.SessionNotFound) -> JSONResponse:
    return JSONResponse({"detail": "세션을 찾을 수 없습니다."}, status_code=404)


@app.get("/api/health")
def health() -> dict:
    return {
        "llm_model": config.llm_model(),
        "api_key_set": config.api_key_set(),
        "whisper_model": config.whisper_model(),
        # 모델이 올라간 뒤에는 실제 장치(cuda 또는 cpu)가 나온다.
        "whisper_device": stt.status()["device"],
        "whisper_loaded": stt.status()["loaded"],
        "records": [p.name for p in config.record_files()],
    }


def _transcribe_upload(audio: UploadFile) -> str:
    """녹음을 임시 파일로 받아 전사하고, 성공하든 실패하든 바로 지운다."""
    data = audio.file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "녹음 파일이 너무 큽니다.")
    if not data:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    try:
        return stt.transcribe(path)
    finally:
        path.unlink(missing_ok=True)


@app.post("/api/stt/test")
def stt_test(audio: UploadFile = File(...)) -> dict:
    """마이크 테스트: 짧은 녹음을 전사해 돌려준다."""
    return {"transcript": _transcribe_upload(audio), "device": stt.status()["device"]}


@app.get("/api/setup")
def setup() -> dict:
    """설정 화면에 필요한 것: 학교 모드, 생기부 파일 상태, 기본값."""
    try:
        loaded = store.load_record(interviewer.transcribe_image)
        record = {"ok": True, "chars": len(loaded.text), "sources": loaded.sources}
    except (store.RecordError, interviewer.LLMError) as e:
        record = {"ok": False, "error": str(e), "sources": []}
    return {
        "llm_model": config.llm_model(),
        "api_key_set": config.api_key_set(),
        "stt": stt.status(),
        "max_questions": MAX_QUESTIONS,
        "schools": [s.to_public() for s in SCHOOLS.values()],
        "answer_limit": {
            "default": DEFAULT_ANSWER_LIMIT_SEC,
            "min": ANSWER_LIMIT_MIN_SEC,
            "max": ANSWER_LIMIT_MAX_SEC,
        },
        "mask": {
            "name_set": bool(config.mask_name()),
            "school_set": bool(config.mask_school()),
        },
        "record": record,
    }


class SessionCreate(BaseModel):
    school: str
    answer_limit_sec: int = Field(
        DEFAULT_ANSWER_LIMIT_SEC, ge=ANSWER_LIMIT_MIN_SEC, le=ANSWER_LIMIT_MAX_SEC
    )


def _ask(session: store.Session) -> store.Turn:
    """다음 질문을 받아 세션에 붙인다."""
    school = get_school(session.school)
    remaining = max(0, round(session.total_limit_sec - session.elapsed_sec))
    # 이 질문에 답하고 나면 다음 질문을 할 시간이 남지 않거나, 질문 수가 다 찼을 때.
    after_answer = remaining - session.answer_limit_sec - QUESTION_READ_SEC
    is_last = bool(session.turns) and (
        len(session.turns) + 1 >= MAX_QUESTIONS or after_answer < MIN_REMAINING_SEC
    )
    question = interviewer.next_question(
        school,
        session.record_text,
        session.turn_dicts(),
        remaining_sec=remaining,
        answer_limit_sec=session.answer_limit_sec,
        is_last=is_last,
    )
    return session.add_question(question)


def _should_finish(session: store.Session) -> bool:
    remaining = session.total_limit_sec - session.elapsed_sec
    return len(session.turns) >= MAX_QUESTIONS or remaining < MIN_REMAINING_SEC


# 라우트는 동기 def로 둔다. LLM과 Whisper 호출이 스레드풀에서 돌아 이벤트 루프를 막지 않는다.


@app.post("/api/sessions")
def create_session(body: SessionCreate) -> dict:
    try:
        school = get_school(body.school)
    except ValueError as e:
        raise HTTPException(400, str(e))
    record = store.load_record(interviewer.transcribe_image)
    session = store.new_session(
        school.key, school.total_limit_sec, body.answer_limit_sec, record.text
    )
    turn = _ask(session)
    store.save_session(session)
    return {
        "session_id": session.id,
        "total_limit_sec": session.total_limit_sec,
        "answer_limit_sec": session.answer_limit_sec,
        "question": turn.to_question(),
    }


@app.post("/api/sessions/{session_id}/answers")
def submit_answer(
    session_id: str,
    answer_sec: float = Form(..., ge=0),
    elapsed_sec: float = Form(..., ge=0),
    text: str = Form(""),
    audio: UploadFile | None = File(None),
) -> dict:
    session = store.load_session(session_id)
    if session.done:
        raise HTTPException(409, "이미 끝난 면접입니다.")
    turn = session.last_turn

    # 답변은 질문 생성보다 먼저 저장한다. 질문 생성이 실패해 프론트가 같은 요청을 다시
    # 보내면 저장된 답변을 그대로 두고 질문 생성만 다시 한다.
    if turn.answer is None:
        if audio is not None:
            text = _transcribe_upload(audio)
        turn.answer = text.strip() or store.NO_ANSWER
        turn.answer_sec = answer_sec
        session.elapsed_sec = max(session.elapsed_sec, elapsed_sec)
        store.save_session(session)

    if _should_finish(session):
        session.done = True
        store.save_session(session)
        return {"transcript": turn.answer, "done": True}

    next_turn = _ask(session)
    store.save_session(session)
    return {"transcript": turn.answer, "done": False, "question": next_turn.to_question()}


@app.post("/api/sessions/{session_id}/report")
def create_report(session_id: str) -> dict:
    session = store.load_session(session_id)
    if not session.done:
        raise HTTPException(409, "면접이 아직 끝나지 않았습니다.")
    # 한 번 만든 리포트는 다시 만들지 않는다. 새로고침이나 재시도로 비용이 두 번 들지 않는다.
    if session.report is None:
        session.report = interviewer.generate_report(
            get_school(session.school),
            session.record_text,
            session.turn_dicts(),
            session.answer_limit_sec,
        )
        store.save_session(session)
    return {"report": session.report}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    return store.load_session(session_id).to_public()


# API 라우트를 모두 등록한 뒤에 마운트해야 /api/* 가 정적 파일에 가려지지 않는다.
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
