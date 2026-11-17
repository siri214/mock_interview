"""LLM 호출: 이미지 전사, 질문·꼬리질문 생성. 호출은 모두 _create 한 곳을 지난다."""

import base64
import json
import logging
import time
from pathlib import Path

import anthropic

from app import config, prompts
from app.schools import INTERVIEWERS, School

log = logging.getLogger("interview.llm")

# 안전 분류기가 요청을 거절하면 서버가 같은 요청을 다른 모델로 다시 실행하게 한다.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-fable-5-2",
    "claude-fable-5-1",
}

# 질문 사이 대기 시간을 줄이려고 낮게 둔다. 질문 품질이 아쉬우면 medium으로 올린다.
QUESTION_EFFORT = "low"
# 리포트는 기다릴 수 있으니 더 깊게 본다.
REPORT_EFFORT = "medium"

_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

_client: anthropic.Anthropic | None = None


class LLMError(Exception):
    """프론트에 그대로 보여줄 수 있는 원인 메시지와 HTTP 상태 코드."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def _get_client() -> anthropic.Anthropic:
    global _client
    if not config.api_key_set():
        raise LLMError(".env에 ANTHROPIC_API_KEY가 없습니다.", 503)
    if _client is None:
        _client = anthropic.Anthropic(timeout=120.0)
    return _client


def _create(*, purpose: str, **kwargs) -> anthropic.types.Message:
    client = _get_client()
    model = config.llm_model()
    started = time.monotonic()
    try:
        if model in FALLBACK_MODELS:
            response = client.beta.messages.create(
                model=model, betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        else:
            response = client.messages.create(model=model, **kwargs)
    except anthropic.AuthenticationError:
        raise LLMError("API 키가 올바르지 않습니다. .env의 ANTHROPIC_API_KEY를 확인하세요.")
    except anthropic.PermissionDeniedError as e:
        raise LLMError(f"API 키에 권한이 없습니다: {e.message}")
    except anthropic.NotFoundError:
        raise LLMError(f"모델 '{model}'을 찾을 수 없습니다. LLM_MODEL을 확인하세요.")
    except anthropic.RateLimitError:
        raise LLMError("API 요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.", 503)
    except anthropic.BadRequestError as e:
        # 크레딧 부족도 이 오류로 온다.
        raise LLMError(f"API가 요청을 거부했습니다: {e.message}")
    except anthropic.APIStatusError as e:
        raise LLMError(f"API 오류({e.status_code})입니다. 잠시 후 다시 시도하세요.", 503)
    except anthropic.APIConnectionError:
        raise LLMError("API에 연결하지 못했습니다. 인터넷 연결을 확인하세요.", 503)

    usage = response.usage
    # 본문은 찍지 않는다. 캐시가 적용되는지는 cache_read 값으로 확인한다.
    log.info(
        "%s model=%s %.1fs input=%s cache_write=%s cache_read=%s output=%s",
        purpose,
        response.model,
        time.monotonic() - started,
        usage.input_tokens,
        usage.cache_creation_input_tokens,
        usage.cache_read_input_tokens,
        usage.output_tokens,
    )
    if response.stop_reason == "refusal":
        raise LLMError("모델이 이 요청에 응답하지 않았습니다. 다시 시도하세요.")
    if response.stop_reason == "max_tokens":
        raise LLMError("모델 응답이 중간에 잘렸습니다. 다시 시도하세요.")
    return response


def _text(response: anthropic.types.Message) -> str:
    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text.strip():
        raise LLMError("모델이 빈 응답을 돌려주었습니다. 다시 시도하세요.")
    return text


def transcribe_image(path: Path) -> str:
    """수상경력 캡처 이미지를 텍스트로 옮긴다. 결과 캐시는 호출한 쪽이 맡는다."""
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    response = _create(
        purpose="transcribe_image",
        max_tokens=8000,
        output_config={"effort": "low"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _MEDIA_TYPES[path.suffix.lower()],
                            "data": data,
                        },
                    },
                    {"type": "text", "text": prompts.TRANSCRIBE_IMAGE},
                ],
            }
        ],
    )
    return _text(response).strip()


def _system(school: School, record_text: str) -> list[dict]:
    return [
        {"type": "text", "text": prompts.interviewer_system(school)},
        {
            "type": "text",
            "text": prompts.record_block(record_text),
            # 생기부까지가 세션 내내 같은 접두부다. 여기까지 캐시한다.
            "cache_control": {"type": "ephemeral"},
        },
    ]


def followup_count(turns: list[dict]) -> int:
    """현재 주제에서 이어진 꼬리질문 수."""
    count = 0
    for turn in reversed(turns):
        if turn["type"] != "followup":
            break
        count += 1
    return count


def assign_interviewer(turns: list[dict], question_type: str) -> str:
    """새 질문은 면접위원을 번갈아 맡고, 꼬리질문은 묻던 사람이 이어 간다."""
    if not turns:
        return INTERVIEWERS[0]
    last = turns[-1]["interviewer"]
    if question_type == "followup":
        return last
    return INTERVIEWERS[(INTERVIEWERS.index(last) + 1) % len(INTERVIEWERS)]


def next_question(
    school: School,
    record_text: str,
    turns: list[dict],
    remaining_sec: int,
    answer_limit_sec: int,
    is_last: bool = False,
    covered: list[str] | None = None,
) -> dict:
    """다음 질문 {type, competency, evidence, text, interviewer}를 만든다."""
    response = _create(
        purpose="next_question",
        max_tokens=16000,
        system=_system(school, record_text),
        output_config={
            "effort": QUESTION_EFFORT,
            "format": {"type": "json_schema", "schema": prompts.question_schema(school)},
        },
        messages=[
            {
                "role": "user",
                "content": prompts.question_request(
                    turns, remaining_sec, answer_limit_sec, followup_count(turns), is_last, covered
                ),
            }
        ],
    )
    try:
        question = json.loads(_text(response))
    except json.JSONDecodeError:
        raise LLMError("모델 응답을 해석하지 못했습니다. 다시 시도하세요.")
    if not turns:
        question["type"] = "new"
    question["interviewer"] = assign_interviewer(turns, question["type"])
    return question


def _normalize_report(school: School, raw: dict, turns: list[dict]) -> dict:
    """점수 순서와 비중은 학교 설정을 따른다. 비중은 모델이 아니라 설정에서 넣는다."""
    by_name = {s["competency"]: s for s in raw["scores"]}
    scores = []
    for competency in school.competencies:
        item = by_name.get(competency.name)
        if item is None:
            raise LLMError("리포트에 빠진 평가요소가 있습니다. 다시 시도하세요.")
        score = min(5.0, max(1.0, round(float(item["score_5"]) * 2) / 2))
        scores.append(
            {
                "competency": competency.name,
                "weight": round(competency.weight, 1),
                "score_5": score,
                "reason": item["reason"],
            }
        )
    valid = {t["index"] for t in turns}
    per_question = sorted(
        {q["index"]: q for q in raw["per_question"] if q["index"] in valid}.values(),
        key=lambda q: q["index"],
    )
    return {
        "overall": raw["overall"],
        "scores": scores,
        "per_question": per_question,
        "delivery": raw["delivery"],
        "next_practice": raw["next_practice"][:3],
    }


def generate_report(
    school: School, record_text: str, turns: list[dict], answer_limit_sec: int
) -> dict:
    """면접이 끝난 뒤의 평가 리포트.

    출력 스키마가 질문 생성과 달라 프롬프트 캐시는 이어지지 않는다(실측). 리포트마다 생기부
    전체를 한 번 더 캐시에 쓰는 비용이 든다.
    """
    response = _create(
        purpose="report",
        max_tokens=16000,
        system=_system(school, record_text),
        output_config={
            "effort": REPORT_EFFORT,
            "format": {"type": "json_schema", "schema": prompts.report_schema(school)},
        },
        messages=[
            {
                "role": "user",
                "content": prompts.report_request(school, turns, answer_limit_sec),
            }
        ],
    )
    try:
        raw = json.loads(_text(response))
    except json.JSONDecodeError:
        raise LLMError("모델 응답을 해석하지 못했습니다. 다시 시도하세요.")
    return _normalize_report(school, raw, turns)


def retry_feedback(
    school: School,
    record_text: str,
    turn: dict,
    previous_answer: str,
    previous_sec: float,
    previous_feedback: dict | None,
    new_answer: str,
    new_sec: float,
    answer_limit_sec: int,
) -> dict:
    """같은 질문에 다시 한 답변의 피드백 {progress, good, improve, direction, mismatch}."""
    response = _create(
        purpose="retry_feedback",
        max_tokens=8000,
        system=_system(school, record_text),
        output_config={
            "effort": REPORT_EFFORT,
            "format": {"type": "json_schema", "schema": prompts.RETRY_SCHEMA},
        },
        messages=[
            {
                "role": "user",
                "content": prompts.retry_request(
                    turn, previous_answer, previous_sec, previous_feedback,
                    new_answer, new_sec, answer_limit_sec,
                ),
            }
        ],
    )
    try:
        return json.loads(_text(response))
    except json.JSONDecodeError:
        raise LLMError("모델 응답을 해석하지 못했습니다. 다시 시도하세요.")
