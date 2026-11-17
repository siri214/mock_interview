"""생기부 마스킹. 이 함수를 거친 텍스트만 세션 기록과 외부 API에 닿는다."""

import re

NAME_TOKEN = "[지원자]"
SCHOOL_TOKEN = "[고등학교]"
REMOVED_TOKEN = "[삭제]"

# 주민등록번호: 6자리-7자리, 뒷자리 첫 숫자는 1~8
_RRN = re.compile(r"(?<!\d)\d{6}\s*-\s*[1-8]\d{6}(?!\d)")
# 휴대전화와 유선전화. 구분자는 하이픈, 점, 공백이거나 없다.
_MOBILE = re.compile(r"(?<!\d)01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
_LANDLINE = re.compile(r"(?<!\d)0\d{1,2}[-.)]\s?\d{3,4}[-.]\d{4}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# 긴 접미사부터 떼어 학교명의 어간을 구한다.
_SCHOOL_SUFFIXES = ("여자고등학교", "고등학교", "여고", "고")
_OPTIONAL_SUFFIX = r"(?:\s*여\s*자\s*고\s*등\s*학\s*교|\s*고\s*등\s*학\s*교|\s*여\s*고)?"


def _spaced(text: str) -> str:
    """PDF 추출에서 글자 사이에 공백이 끼어도 잡히도록 한다."""
    return r"\s*".join(re.escape(ch) for ch in text if not ch.isspace())


def _name_pattern(name: str) -> re.Pattern | None:
    compact = "".join(name.split())
    if len(compact) < 2:
        return None
    return re.compile(_spaced(compact))


def _school_pattern(school: str) -> re.Pattern | None:
    """정식 명칭과 줄임말을 함께 잡는다. 예: 한빛고등학교, 한빛고 / 한빛여자고등학교, 한빛여고."""
    compact = "".join(school.split())
    if len(compact) < 2:
        return None
    stem, girls = compact, False
    for suffix in _SCHOOL_SUFFIXES:
        if compact.endswith(suffix) and len(compact) > len(suffix):
            stem = compact[: -len(suffix)]
            girls = suffix.startswith("여")
            break
    else:
        # 접미사 없이 "한빛"처럼 앞부분만 입력한 경우. 바로 뒤에 학교 접미사가 붙어 있으면
        # 함께 지워 "[고등학교]여자고등학교"처럼 남지 않게 한다.
        return re.compile(_spaced(compact) + _OPTIONAL_SUFFIX)
    if girls:
        tail = r"\s*여\s*(?:자\s*고\s*등\s*학\s*교|고)"
    else:
        tail = r"\s*고(?:\s*등\s*학\s*교)?"
    return re.compile(_spaced(stem) + tail)


def mask(text: str, name: str | None = None, school: str | None = None) -> str:
    """이름은 [지원자], 학교명은 [고등학교]로 바꾸고 주민번호·전화번호·이메일은 지운다."""
    if school and (pattern := _school_pattern(school)):
        text = pattern.sub(SCHOOL_TOKEN, text)
    if name and (pattern := _name_pattern(name)):
        text = pattern.sub(NAME_TOKEN, text)
    for pattern in (_RRN, _EMAIL, _MOBILE, _LANDLINE):
        text = pattern.sub(REMOVED_TOKEN, text)
    return text
