import pytest

from app.masking import NAME_TOKEN, REMOVED_TOKEN, SCHOOL_TOKEN, mask
from app.schools import SCHOOLS, get_school


def test_name_is_replaced():
    out = mask("김하늘 학생은 실험을 주도함. 김하늘은 조장을 맡음.", name="김하늘")
    assert "김하늘" not in out
    assert out.count(NAME_TOKEN) == 2


def test_name_with_spaces_from_pdf_extraction():
    assert mask("성명: 김 하 늘", name="김하늘") == f"성명: {NAME_TOKEN}"


def test_single_character_name_is_ignored():
    text = "김 씨가 발표함"
    assert mask(text, name="김") == text


def test_school_full_and_short_forms():
    out = mask("한빛고등학교장 / 한빛고 과학동아리 / 한빛 고등학교", school="한빛고등학교")
    assert "한빛" not in out
    assert out.count(SCHOOL_TOKEN) == 3


def test_school_entered_as_short_form():
    out = mask("한빛고등학교 교내 대회", school="한빛고")
    assert out == f"{SCHOOL_TOKEN} 교내 대회"


def test_girls_school_forms():
    out = mask("한빛여자고등학교, 한빛여고", school="한빛여고")
    assert out == f"{SCHOOL_TOKEN}, {SCHOOL_TOKEN}"


def test_school_entered_as_stem_only():
    out = mask("한빛여자고등학교장, 한빛 고등학교, 한빛여고, 한빛 축제", school="한빛")
    assert out == f"{SCHOOL_TOKEN}장, {SCHOOL_TOKEN}, {SCHOOL_TOKEN}, {SCHOOL_TOKEN} 축제"


def test_school_without_suffix_matches_literally():
    out = mask("한빛과학영재원 수료", school="한빛과학영재원")
    assert out == f"{SCHOOL_TOKEN} 수료"


@pytest.mark.parametrize(
    "secret",
    [
        "080101-3123456",
        "080101 - 4123456",
        "010-1234-5678",
        "01012345678",
        "010.1234.5678",
        "031-123-4567",
        "02)1234-5678",
        "haneul.kim@example.co.kr",
    ],
)
def test_patterns_are_removed(secret):
    out = mask(f"연락처 {secret} 입니다")
    assert out == f"연락처 {REMOVED_TOKEN} 입니다"


@pytest.mark.parametrize(
    "text",
    [
        "2025.03.02.부터 2026.02.28.까지",
        "2025-03-02 교내 과학탐구대회 금상",
        "밀도 범함수 이론으로 밴드갭 1.12 eV를 계산함",
        "출결: 수업일수 190일, 결석 0",
        "시료 0.5 g을 1000 mL 용액에 녹임",
    ],
)
def test_record_text_is_left_alone(text):
    assert mask(text, name="김하늘", school="한빛고등학교") == text


def test_no_inputs_only_strips_patterns():
    assert mask("김하늘 한빛고") == "김하늘 한빛고"


def test_school_weights_sum_to_100():
    for school in SCHOOLS.values():
        assert sum(c.weight for c in school.competencies) == pytest.approx(100)


def test_school_settings():
    ajou, inha = get_school("ajou"), get_school("inha")
    assert ajou.total_limit_sec == 600
    assert [c.weight for c in ajou.competencies] == [80, 20]
    assert inha.total_limit_sec == 540
    assert len(inha.competencies) == 3
    assert len(inha.example_questions) == 3
    with pytest.raises(ValueError):
        get_school("snu")
