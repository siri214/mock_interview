"""LLM을 가짜로 바꾸고 세션 한 판을 끝까지 돌린다."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, interviewer, session as store, stt
from app.main import app
from app.schools import MAX_QUESTIONS, get_school

FIXTURE = Path(__file__).parent / "fixtures" / "records" / "sample_record.txt"
EVIDENCE = "60도에서 가장 균일한 막이 생겼다고 보고서에 정리하고"


@pytest.fixture
def calls():
    return []


@pytest.fixture
def client(tmp_path, monkeypatch, calls):
    records = tmp_path / "records"
    records.mkdir()
    shutil.copy(FIXTURE, records)
    monkeypatch.setattr(config, "RECORDS_DIR", records)
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setenv("MASK_NAME", "김하늘")
    monkeypatch.setenv("MASK_SCHOOL", "한빛고등학교")

    def fake_next_question(school, record_text, turns, remaining_sec, answer_limit_sec, is_last, covered):
        calls.append(
            {
                "record_text": record_text,
                "turns": turns,
                "remaining_sec": remaining_sec,
                "is_last": is_last,
                "covered": covered,
            }
        )
        # 두 번째 질문만 꼬리질문으로 돌려준다.
        qtype = "followup" if len(turns) == 1 else "new"
        return {
            "type": qtype,
            "competency": school.competencies[0].name,
            "evidence": EVIDENCE if len(turns) != 2 else "생기부에 없는 문장",
            "text": f"질문 {len(turns) + 1}입니다.",
            "interviewer": interviewer.assign_interviewer(turns, qtype),
        }

    monkeypatch.setattr(interviewer, "next_question", fake_next_question)
    monkeypatch.setattr(interviewer, "_create", fake_report_create)
    return TestClient(app)


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Response:
    def __init__(self, payload):
        self.content = [_Block(json.dumps(payload, ensure_ascii=False))]


REPORT_CALLS = []


def fake_report_create(*, purpose, **kwargs):
    """모델 응답을 흉내 낸다. 평가요소 순서를 뒤집고 범위를 벗어난 값을 섞어 정리 로직을 본다."""
    assert purpose == "report"
    REPORT_CALLS.append(kwargs)
    names = kwargs["output_config"]["format"]["schema"]["properties"]["scores"]["items"][
        "properties"
    ]["competency"]["enum"]
    return _Response(
        {
            "overall": "총평",
            "scores": [
                {"competency": n, "score_5": 3.74 + i * 5, "reason": f"이유 {n}"}
                for i, n in enumerate(reversed(names))
            ],
            "per_question": [
                {"index": 99, "good": "", "improve": "", "direction": "", "mismatch": None},
                {"index": 1, "good": "g", "improve": "i", "direction": "d", "mismatch": None},
            ],
            "delivery": "전달",
            "next_practice": ["하나", "둘", "셋", "넷"],
        }
    )


def start(client, school="ajou", **extra):
    res = client.post("/api/sessions", json={"school": school, **extra})
    assert res.status_code == 200, res.text
    return res.json()


def answer(client, sid, text, answer_sec=60, elapsed_sec=70):
    return client.post(
        f"/api/sessions/{sid}/answers",
        data={"text": text, "answer_sec": answer_sec, "elapsed_sec": elapsed_sec},
    )


def test_create_session_masks_record_and_returns_first_question(client, calls):
    body = start(client)
    assert body["total_limit_sec"] == 600
    assert body["answer_limit_sec"] == 90
    assert body["question"] == {"index": 1, "interviewer": "A", "text": "질문 1입니다."}

    sent = calls[0]["record_text"]
    assert "김하늘" not in sent and "한빛고" not in sent and "010-1234-5678" not in sent
    assert "[지원자]" in sent and "[고등학교]" in sent

    saved = store.load_session(body["session_id"])
    assert "김하늘" not in saved.record_text


def test_inha_limits_and_custom_answer_limit(client):
    body = start(client, school="inha", answer_limit_sec=60)
    assert body["total_limit_sec"] == 540
    assert body["answer_limit_sec"] == 60


def test_unknown_school_is_rejected(client):
    assert client.post("/api/sessions", json={"school": "snu"}).status_code == 400


def test_followup_keeps_interviewer_and_new_question_alternates(client):
    sid = start(client)["session_id"]
    q2 = answer(client, sid, "첫 답변", elapsed_sec=70).json()["question"]
    q3 = answer(client, sid, "둘째 답변", elapsed_sec=140).json()["question"]
    assert q2["interviewer"] == "A"  # 꼬리질문은 묻던 사람이 이어 간다
    assert q3["interviewer"] == "B"

    session = client.get(f"/api/sessions/{sid}").json()
    assert "record_text" not in session
    assert [t["type"] for t in session["turns"]] == ["new", "followup", "new"]
    assert [t["evidence_verified"] for t in session["turns"]] == [True, True, False]
    assert session["turns"][0]["answer"] == "첫 답변"
    assert session["turns"][0]["answer_sec"] == 60


def test_finishes_when_time_runs_out(client, calls):
    sid = start(client)["session_id"]
    res = answer(client, sid, "답변", elapsed_sec=570).json()  # 남은 시간 30초 < 40초
    assert res == {"transcript": "답변", "done": True}
    assert len(calls) == 1
    assert answer(client, sid, "또 답변").status_code == 409


def test_finishes_at_max_questions(client):
    sid = start(client)["session_id"]
    for i in range(1, MAX_QUESTIONS):
        res = answer(client, sid, f"답변 {i}", elapsed_sec=i * 10).json()
        assert res["done"] is False
    res = answer(client, sid, "마지막", elapsed_sec=100).json()
    assert res["done"] is True
    assert len(client.get(f"/api/sessions/{sid}").json()["turns"]) == MAX_QUESTIONS


def test_empty_answer_is_recorded_as_no_answer(client):
    sid = start(client)["session_id"]
    assert answer(client, sid, "   ").json()["transcript"] == store.NO_ANSWER


def test_retry_after_llm_failure_does_not_duplicate_answer(client, monkeypatch):
    sid = start(client)["session_id"]
    working = interviewer.next_question

    def failing(*args, **kwargs):
        raise interviewer.LLMError("API 요청 한도에 걸렸습니다.", 503)

    monkeypatch.setattr(interviewer, "next_question", failing)
    res = answer(client, sid, "원래 답변", elapsed_sec=70)
    assert res.status_code == 503
    assert res.json()["detail"] == "API 요청 한도에 걸렸습니다."

    monkeypatch.setattr(interviewer, "next_question", working)
    res = answer(client, sid, "다시 보낸 답변", elapsed_sec=75).json()
    assert res["transcript"] == "원래 답변"
    turns = client.get(f"/api/sessions/{sid}").json()["turns"]
    assert len(turns) == 2 and turns[0]["answer"] == "원래 답변"


def test_bad_session_id(client):
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.get("/api/sessions/" + "0" * 32).status_code == 404


def test_image_is_transcribed_once_and_placed_first(client, monkeypatch):
    image = config.RECORDS_DIR / "awards.png"
    image.write_bytes(b"not a real image")
    seen = []

    def fake_transcribe(path):
        seen.append(path.name)
        return "교내 과학탐구대회 | 금상 | 한빛고등학교장"

    record = store.load_record(fake_transcribe)
    again = store.load_record(fake_transcribe)
    assert seen == ["awards.png"]  # 두 번째는 캐시에서 읽는다
    assert record.text == again.text
    assert record.text.startswith("[캡처 이미지 전사]\n교내 과학탐구대회 | 금상 | [고등학교]장")
    assert [s["kind"] for s in record.sources] == ["image", "text"]
    assert [p.name for p in config.record_files()] == ["awards.png", "sample_record.txt"]


def test_evidence_check_ignores_whitespace():
    record = "은거울 반응 실험에서\n반응 온도에 따라 은 박막이"
    assert store.evidence_in_record("은거울 반응 실험에서 반응 온도에 따라", record)
    assert not store.evidence_in_record("금거울 반응", record)
    assert not store.evidence_in_record("  ", record)


def finish(client, school):
    sid = start(client, school=school)["session_id"]
    assert answer(client, sid, "답변", elapsed_sec=570).json()["done"] is True
    return sid


@pytest.mark.parametrize(
    ("school", "weights"), [("ajou", [80, 20]), ("inha", [33.3, 33.3, 33.3])]
)
def test_report_uses_school_weights(client, school, weights):
    sid = finish(client, school)
    report = client.post(f"/api/sessions/{sid}/report").json()["report"]
    assert [s["weight"] for s in report["scores"]] == weights
    # 순서는 학교 설정을 따르고, 점수는 0.5 단위로 1~5 안에 들어온다.
    assert [s["score_5"] for s in report["scores"]][-1] == 3.5
    assert all(1 <= s["score_5"] <= 5 for s in report["scores"])
    assert [q["index"] for q in report["per_question"]] == [1]
    assert report["next_practice"] == ["하나", "둘", "셋"]
    assert client.get(f"/api/sessions/{sid}").json()["report"] == report


def test_report_is_generated_once(client):
    sid = finish(client, "ajou")
    REPORT_CALLS.clear()
    first = client.post(f"/api/sessions/{sid}/report").json()
    second = client.post(f"/api/sessions/{sid}/report").json()
    assert first == second
    assert len(REPORT_CALLS) == 1


def test_report_requires_finished_interview(client):
    sid = start(client)["session_id"]
    assert client.post(f"/api/sessions/{sid}/report").status_code == 409


def test_report_prompt_carries_answers_and_durations(client):
    sid = finish(client, "inha")
    REPORT_CALLS.clear()
    client.post(f"/api/sessions/{sid}/report")
    prompt = REPORT_CALLS[0]["messages"][0]["content"]
    assert "<답변 소요=60초>\n답변\n</답변>" in prompt
    assert "기초학업역량(33%)" in prompt
    assert "질문당 90초" in prompt


SPOKEN = "저는 동아리에서 태양전지 소재를 탐구했습니다"  # 한글 20자


def fake_stt(text=SPOKEN, audio_sec=10.0, speech_sec=7.0):
    return lambda path: stt.Transcript(text=text, audio_sec=audio_sec, speech_sec=speech_sec)


def post_audio(client, sid, content=b"fake-audio", **data):
    return client.post(
        f"/api/sessions/{sid}/answers",
        data={"answer_sec": 30, "elapsed_sec": 40, **data},
        files={"audio": ("answer.webm", content, "audio/webm")},
    )


def test_audio_answer_is_transcribed_kept_and_measured(client, monkeypatch):
    monkeypatch.setattr(stt, "transcribe", fake_stt(f"  {SPOKEN} "))
    sid = start(client)["session_id"]
    assert post_audio(client, sid).json()["transcript"] == SPOKEN

    turn = client.get(f"/api/sessions/{sid}").json()["turns"][0]
    assert turn["audio"] == "1.webm"
    assert turn["speech"] == {
        "audio_sec": 10.0, "speech_sec": 7.0, "silence_sec": 3.0, "syllables": 20, "spm": 171,
    }
    res = client.get(f"/api/sessions/{sid}/audio/1.webm")
    assert res.status_code == 200 and res.content == b"fake-audio"
    assert res.headers["content-type"] == "audio/webm"


def test_audio_file_removed_when_transcription_fails(client, monkeypatch):
    def failing(path):
        raise stt.STTError("녹음을 읽지 못했습니다.")

    monkeypatch.setattr(stt, "transcribe", failing)
    sid = start(client)["session_id"]
    res = post_audio(client, sid, content=b"broken")
    assert res.status_code == 500 and res.json()["detail"] == "녹음을 읽지 못했습니다."
    assert not (config.AUDIO_DIR / sid / "1.webm").exists()
    # 답변이 저장되지 않았으므로 다시 보낼 수 있다.
    assert client.get(f"/api/sessions/{sid}").json()["turns"][0]["answer"] is None


def test_text_answer_has_no_audio_or_speech(client):
    sid = start(client)["session_id"]
    answer(client, sid, "글 답변")
    turn = client.get(f"/api/sessions/{sid}").json()["turns"][0]
    assert turn["audio"] is None and turn["speech"] is None


def test_audio_endpoint_rejects_bad_names(client):
    sid = start(client)["session_id"]
    for name in ["2.webm", "..%2F..%2Fsecret.webm", "1.mp3", "x.webm"]:
        assert client.get(f"/api/sessions/{sid}/audio/{name}").status_code == 404
    with pytest.raises(store.SessionNotFound):
        store.audio_path(sid, "../1.webm")


def test_speech_stats_skips_short_speech():
    assert store.speech_stats("네", 3.0, 1.0) is None
    assert store.speech_stats("", 30.0, 20.0) is None
    assert store.speech_stats("가나다라마 12 ab!", 20.0, 6.0)["syllables"] == 9


def test_report_prompt_includes_speech_rate(client, monkeypatch):
    monkeypatch.setattr(stt, "transcribe", fake_stt())
    sid = start(client)["session_id"]
    post_audio(client, sid, elapsed_sec=570)
    REPORT_CALLS.clear()
    client.post(f"/api/sessions/{sid}/report")
    assert "<답변 소요=30초 말하기속도=분당171음절 침묵=3초>" in REPORT_CALLS[0]["messages"][0]["content"]


def test_retry_with_audio_keeps_recording_per_attempt(client, monkeypatch):
    monkeypatch.setattr(stt, "transcribe", fake_stt())
    monkeypatch.setattr(
        interviewer, "retry_feedback",
        lambda *a: {"progress": "p", "good": "g", "improve": "i", "direction": "d", "mismatch": None},
    )
    sid = finish(client, "ajou")
    url = f"/api/sessions/{sid}/turns/1/retry"
    for _ in range(2):
        res = client.post(url, data={"answer_sec": 20}, files={"audio": ("a.webm", b"again", "audio/webm")})
    assert res.json()["attempt"]["audio"] == "1-r2.webm" and res.json()["attempt"]["speech"]["spm"] == 171
    assert client.get(f"/api/sessions/{sid}/audio/1-r1.webm").content == b"again"

    def failing(*a):
        raise interviewer.LLMError("한도", 503)

    monkeypatch.setattr(interviewer, "retry_feedback", failing)
    res = client.post(url, data={"answer_sec": 20}, files={"audio": ("a.webm", b"again", "audio/webm")})
    assert res.status_code == 503
    assert not (config.AUDIO_DIR / sid / "1-r3.webm").exists()


def test_empty_audio_is_no_answer(client, monkeypatch):
    monkeypatch.setattr(stt, "transcribe", lambda path: pytest.fail("전사하면 안 된다"))
    sid = start(client)["session_id"]
    res = client.post(
        f"/api/sessions/{sid}/answers",
        data={"answer_sec": 90, "elapsed_sec": 100},
        files={"audio": ("answer.webm", b"", "audio/webm")},
    ).json()
    assert res["transcript"] == store.NO_ANSWER


def test_mic_test_endpoint(client, monkeypatch):
    monkeypatch.setattr(stt, "transcribe", fake_stt("마이크 테스트"))
    res = client.post("/api/stt/test", files={"audio": ("t.webm", b"x", "audio/webm")}).json()
    assert res["transcript"] == "마이크 테스트"


def test_last_question_is_flagged_by_time(client, calls):
    sid = start(client)["session_id"]  # 600초, 답변 제한 90초
    answer(client, sid, "답변", elapsed_sec=100)
    answer(client, sid, "답변", elapsed_sec=470)  # 남은 130초: 답하고 나면 다음 질문 시간이 없다
    assert [c["is_last"] for c in calls] == [False, False, True]


def test_last_question_is_flagged_by_count(client, calls):
    sid = start(client)["session_id"]
    for i in range(1, MAX_QUESTIONS):
        answer(client, sid, "답변", elapsed_sec=i)
    assert [c["is_last"] for c in calls] == [False] * (MAX_QUESTIONS - 1) + [True]


def test_question_without_evidence_counts_as_verified():
    session = store.new_session("ajou", 600, 90, "생기부 본문")
    turn = session.add_question(
        {"interviewer": "A", "type": "new", "competency": "c", "evidence": "", "text": "지원 동기"}
    )
    assert turn.evidence_verified is True


def test_history_lists_finished_sessions_with_overall_score(client):
    assert client.get("/api/sessions").json() == {"sessions": []}
    start(client)  # 끝내지 않은 세션은 목록에 나오지 않는다
    sid = finish(client, "ajou")
    listed = client.get("/api/sessions").json()["sessions"]
    assert [s["id"] for s in listed] == [sid]
    assert listed[0]["overall"] is None and listed[0]["questions"] == 1

    client.post(f"/api/sessions/{sid}/report")
    item = client.get("/api/sessions").json()["sessions"][0]
    scores = {s["competency"]: s["score_5"] for s in item["scores"]}
    expected = sum(scores[c.name] * c.weight for c in get_school("ajou").competencies) / 100
    assert item["overall"] == pytest.approx(expected, abs=0.01)


def test_next_session_avoids_evidence_from_finished_sessions(client, calls):
    finish(client, "ajou")
    assert calls[0]["covered"] == []
    start(client, school="inha")
    assert calls[-1]["covered"] == [EVIDENCE]


def test_covered_evidence_dedupes_and_ignores_empty(client):
    for _ in range(2):
        finish(client, "ajou")
    assert store.covered_evidence() == [EVIDENCE]


def test_old_session_files_without_new_fields_still_load(client):
    sid = finish(client, "ajou")
    path = config.SESSIONS_DIR / f"{sid}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["covered"]
    for turn in data["turns"]:
        del turn["retries"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loaded = store.load_session(sid)
    assert loaded.covered == [] and loaded.turns[0].retries == []


def test_retry_compares_with_report_feedback_then_with_last_attempt(client, monkeypatch):
    seen = []

    def fake_retry(school, record_text, turn, prev_answer, prev_sec, prev_feedback, answer, sec, limit):
        seen.append({"prev_answer": prev_answer, "prev_feedback": prev_feedback, "answer": answer})
        return {"progress": "p", "good": "g", "improve": "i", "direction": "d", "mismatch": None}

    monkeypatch.setattr(interviewer, "retry_feedback", fake_retry)
    sid = finish(client, "ajou")
    client.post(f"/api/sessions/{sid}/report")

    url = f"/api/sessions/{sid}/turns/1/retry"
    first = client.post(url, data={"text": "두 번째 답변", "answer_sec": 40}).json()
    second = client.post(url, data={"text": "세 번째 답변", "answer_sec": 45}).json()
    assert first["attempts"] == 1 and second["attempts"] == 2
    assert seen[0]["prev_answer"] == "답변" and seen[0]["prev_feedback"]["good"] == "g"
    assert seen[1]["prev_answer"] == "두 번째 답변"

    turn = client.get(f"/api/sessions/{sid}").json()["turns"][0]
    assert [r["answer"] for r in turn["retries"]] == ["두 번째 답변", "세 번째 답변"]
    assert turn["answer"] == "답변"  # 면접 때의 답변은 그대로 둔다


def test_retry_rejects_unfinished_session_and_unknown_question(client):
    sid = start(client)["session_id"]
    assert client.post(f"/api/sessions/{sid}/turns/1/retry", data={"answer_sec": 1}).status_code == 409
    done = finish(client, "ajou")
    assert client.post(f"/api/sessions/{done}/turns/9/retry", data={"answer_sec": 1}).status_code == 404
