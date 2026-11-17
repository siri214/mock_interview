"""환경변수와 경로 설정. 값은 호출 시점에 읽어 .env 변경이 재시작만으로 반영되게 한다."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

STATIC_DIR = ROOT / "static"
# DATA_DIR 환경변수로 바꾸면 샘플 생기부로 서버를 띄울 수 있다.
DATA_DIR = Path(os.getenv("DATA_DIR") or ROOT / "data")
RECORDS_DIR = DATA_DIR / "records"
SESSIONS_DIR = DATA_DIR / "sessions"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
TEXT_EXTENSIONS = {".txt", ".md"}
RECORD_EXTENSIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | {".pdf"}


def llm_model() -> str:
    return os.getenv("LLM_MODEL") or "claude-sonnet-5-5"


def api_key_set() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def whisper_model() -> str:
    return os.getenv("WHISPER_MODEL") or "large-v3-turbo"


def whisper_device() -> str:
    return os.getenv("WHISPER_DEVICE") or "auto"


def record_skip_pages() -> int:
    return int(os.getenv("RECORD_SKIP_PAGES") or 0)


def mask_name() -> str | None:
    return os.getenv("MASK_NAME") or None


def mask_school() -> str | None:
    return os.getenv("MASK_SCHOOL") or None


def record_files() -> list[Path]:
    """data/records/ 바로 아래의 생기부 파일. 숨김 파일과 .cache 폴더는 제외한다."""
    if not RECORDS_DIR.is_dir():
        return []
    return sorted(
        p
        for p in RECORDS_DIR.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() in RECORD_EXTENSIONS
    )
