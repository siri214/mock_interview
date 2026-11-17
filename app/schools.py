"""학교별 면접 모드 설정. 수치의 출처는 plan.md의 '출처' 절."""

from dataclasses import asdict, dataclass

# 공통 규칙
DEFAULT_ANSWER_LIMIT_SEC = 90
MIN_QUESTIONS = 6
MAX_QUESTIONS = 8
# 남은 시간이 이보다 적으면 새 질문 대신 면접을 끝낸다.
MIN_REMAINING_SEC = 40
INTERVIEWERS = ("A", "B")


@dataclass(frozen=True)
class Competency:
    name: str
    weight: float  # 합이 100


@dataclass(frozen=True)
class School:
    key: str
    university: str
    track: str
    department: str
    total_limit_sec: int
    time_note: str
    interviewer_count: int
    competencies: tuple[Competency, ...]
    question_style: str
    ideal_candidate: str
    example_questions: tuple[str, ...] = ()

    def to_public(self) -> dict:
        """설정 화면에 보여줄 값."""
        d = asdict(self)
        for k in ("question_style", "ideal_candidate", "example_questions"):
            d.pop(k)
        return d


AJOU = School(
    key="ajou",
    university="아주대학교",
    track="첨단융합인재",
    department="첨단신소재공학과",
    total_limit_sec=600,
    time_note="10분 내외",
    interviewer_count=2,
    competencies=(
        Competency("서류 신뢰도(학업·진로·공동체역량)", 80),
        Competency("의사소통능력·태도", 20),
    ),
    question_style=(
        "활동의 진위, 과정, 본인 역할, 노력, 결과를 구체적으로 확인한다. "
        "꼬리질문 비중을 높게 둔다."
    ),
    ideal_candidate=(
        "수학·과학 실력 기반의 관심 분야 역량, 전공 준비도, 화학·생명과학 관심"
    ),
)

INHA = School(
    key="inha",
    university="인하대학교",
    track="인하미래인재(면접형)",
    department="신소재공학과",
    total_limit_sec=540,
    time_note="8~10분 내외",
    interviewer_count=2,
    competencies=(
        Competency("기초학업역량", 100 / 3),
        Competency("진로탐구역량", 100 / 3),
        Competency("의사소통역량", 100 / 3),
    ),
    question_style=(
        "세특에 나온 개념을 직접 설명하게 하는 질문, 관심의 계기와 후속 활동, "
        "협업 경험을 묻는다."
    ),
    ideal_candidate="활동 자체보다 의미·학습 과정·탐구 내용의 이해",
    example_questions=(
        "기초학업역량: 전자기 유도 관찰 실험을 설계했다는 물리 세특을 두고, "
        "그 현상을 어떻게 이해하는지 설명하게 한다.",
        "진로탐구역량: 트랜지스터에 꾸준히 관심을 보인 기록에 대해 계기와 "
        "이후 활동을 묻는다.",
        "의사소통역량: 화학 실험 조장 기록을 두고 역할 조율과 참여 유도 방법을 "
        "구체적으로 묻는다.",
    ),
)

SCHOOLS: dict[str, School] = {s.key: s for s in (AJOU, INHA)}


def get_school(key: str) -> School:
    try:
        return SCHOOLS[key]
    except KeyError:
        raise ValueError(f"알 수 없는 학교 모드: {key!r} (가능한 값: {', '.join(SCHOOLS)})")
