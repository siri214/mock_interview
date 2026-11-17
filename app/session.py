"""생기부 파일 읽기와 세션 저장. 이 모듈을 나가는 생기부 텍스트는 항상 마스킹돼 있다."""

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

from app import config
from app.masking import mask

_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_WHITESPACE = re.compile(r"\s+")

NO_ANSWER = "(무응답)"


class RecordError(Exception):
    """생기부를 읽지 못한 이유. 프론트에 그대로 보여준다."""


class SessionNotFound(Exception):
    pass


# ---------- 생기부 ----------


@dataclass
class Record:
    text: str  # 마스킹된 전문
    sources: list[dict]  # 파일별 {name, kind, chars, ...}


def _cache_path(image: Path) -> Path:
    return image.parent / ".cache" / f"{image.name}.txt"


def _read_image(path: Path, transcribe: Callable[[Path], str]) -> tuple[str, dict]:
    cache = _cache_path(path)
    cached = cache.is_file()
    if cached:
        text = cache.read_text(encoding="utf-8")
    else:
        text = transcribe(path)
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    return text, {"kind": "image", "cached": cached}


def _read_pdf(path: Path) -> tuple[str, dict]:
    reader = PdfReader(path)
    total = len(reader.pages)
    skip = min(config.record_skip_pages(), total)
    pages = [reader.pages[i].extract_text() or "" for i in range(skip, total)]
    text = "\n".join(pages)
    if not text.strip():
        raise RecordError(
            f"{path.name}에서 텍스트를 읽지 못했습니다. 스캔본 PDF는 지원하지 않습니다."
        )
    return text, {"kind": "pdf", "pages_total": total, "pages_read": [skip + 1, total]}


def _read_text(path: Path) -> tuple[str, dict]:
    return path.read_text(encoding="utf-8"), {"kind": "text"}


def load_record(transcribe: Callable[[Path], str]) -> Record:
    """data/records/의 파일을 하나의 생기부로 묶는다. 이미지 전사본이 앞, 나머지가 뒤."""
    files = config.record_files()
    if not files:
        raise RecordError("data/records/에 생기부 파일이 없습니다.")
    images = [p for p in files if p.suffix.lower() in config.IMAGE_EXTENSIONS]
    others = [p for p in files if p not in images]

    name, school = config.mask_name(), config.mask_school()
    parts, sources = [], []
    for path in images + others:
        suffix = path.suffix.lower()
        if suffix in config.IMAGE_EXTENSIONS:
            raw, info = _read_image(path, transcribe)
            label = "캡처 이미지 전사"
        elif suffix == ".pdf":
            raw, info = _read_pdf(path)
            label = "PDF 본문"
        else:
            raw, info = _read_text(path)
            label = "텍스트 본문"
        masked = mask(raw, name=name, school=school).strip()
        parts.append(f"[{label}]\n{masked}")
        source = {"name": path.name, "chars": len(masked), **info}
        if info["kind"] == "image":
            source["transcript"] = masked
        sources.append(source)
    return Record(text="\n\n".join(parts), sources=sources)


def evidence_in_record(evidence: str, record_text: str) -> bool:
    """근거 문장이 생기부에 실제로 있는지 본다. PDF 줄바꿈이 섞이므로 공백은 무시한다."""
    needle = _WHITESPACE.sub("", evidence)
    return bool(needle) and needle in _WHITESPACE.sub("", record_text)


# ---------- 세션 ----------


@dataclass
class Turn:
    index: int
    interviewer: str
    type: str
    competency: str
    evidence: str
    evidence_verified: bool
    text: str
    answer: str | None = None
    answer_sec: float | None = None
    # 리포트를 본 뒤 같은 질문에 다시 답한 기록: {answer, answer_sec, feedback}
    retries: list[dict] = field(default_factory=list)

    def to_question(self) -> dict:
        """면접 중에 지원자에게 보여줄 부분."""
        return {"index": self.index, "interviewer": self.interviewer, "text": self.text}


@dataclass
class Session:
    id: str
    school: str
    created_at: str
    total_limit_sec: int
    answer_limit_sec: int
    record_text: str
    turns: list[Turn] = field(default_factory=list)
    elapsed_sec: float = 0
    done: bool = False
    report: dict | None = None
    # 이전 연습에서 이미 물어본 생기부 기록. 세션을 만들 때 한 번 정한다.
    covered: list[str] = field(default_factory=list)

    @property
    def last_turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    def turn_dicts(self) -> list[dict]:
        return [asdict(t) for t in self.turns]

    def add_question(self, question: dict) -> Turn:
        turn = Turn(
            index=len(self.turns) + 1,
            interviewer=question["interviewer"],
            type=question["type"],
            competency=question["competency"],
            evidence=question["evidence"],
            # 지원 동기처럼 근거 기록이 없는 질문은 대조할 것이 없다.
            evidence_verified=not question["evidence"].strip()
            or evidence_in_record(question["evidence"], self.record_text),
            text=question["text"],
        )
        self.turns.append(turn)
        return turn

    def to_public(self) -> dict:
        """생기부 전문을 뺀 세션 내용."""
        data = asdict(self)
        del data["record_text"]
        return data


def new_session(school_key: str, total_limit_sec: int, answer_limit_sec: int, record_text: str) -> Session:
    return Session(
        id=uuid.uuid4().hex,
        school=school_key,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        total_limit_sec=total_limit_sec,
        answer_limit_sec=answer_limit_sec,
        record_text=record_text,
    )


def _session_path(session_id: str) -> Path:
    # 경로에 그대로 쓰이므로 형식을 먼저 확인한다.
    if not _SESSION_ID.match(session_id):
        raise SessionNotFound(session_id)
    return config.SESSIONS_DIR / f"{session_id}.json"


def save_session(session: Session) -> None:
    path = _session_path(session.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(session), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_session(path: Path) -> Session:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["turns"] = [Turn(**t) for t in data["turns"]]
    return Session(**data)


def load_session(session_id: str) -> Session:
    path = _session_path(session_id)
    if not path.is_file():
        raise SessionNotFound(session_id)
    return _read_session(path)


def finished_sessions() -> list[Session]:
    """끝까지 마친 세션을 오래된 것부터 돌려준다."""
    if not config.SESSIONS_DIR.is_dir():
        return []
    sessions = [_read_session(p) for p in config.SESSIONS_DIR.glob("*.json")]
    return sorted((s for s in sessions if s.done), key=lambda s: s.created_at)


MAX_COVERED = 40


def covered_evidence() -> list[str]:
    """이전 연습에서 질문의 근거로 쓰인 생기부 기록. 최근 것 위주로 MAX_COVERED개까지."""
    seen: dict[str, str] = {}
    for session in finished_sessions():
        for turn in session.turns:
            evidence = turn.evidence.strip()
            if evidence:
                key = _WHITESPACE.sub("", evidence)
                seen.pop(key, None)  # 다시 나오면 최근 순서로 옮긴다
                seen[key] = evidence
    return list(seen.values())[-MAX_COVERED:]


def overall_score(report: dict | None) -> float | None:
    """평가요소 점수를 학교별 비중으로 가중 평균한 종합 점수."""
    if not report or not report.get("scores"):
        return None
    total = sum(s["weight"] for s in report["scores"])
    return round(sum(s["score_5"] * s["weight"] for s in report["scores"]) / total, 2)
