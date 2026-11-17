"""faster-whisper 래퍼. cuda를 먼저 시도하고, 실패하면 cpu(int8)로 내려간다."""

import ctypes
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app import config

log = logging.getLogger("interview.stt")

_lock = threading.Lock()
_model = None
_device: str | None = None  # 실제로 올라간 장치
_load_error: str | None = None


@dataclass
class Transcript:
    text: str
    audio_sec: float  # 녹음 전체 길이
    speech_sec: float  # 무음을 뺀, 실제로 말한 시간


class STTError(Exception):
    """프론트에 그대로 보여줄 수 있는 전사 실패 이유."""


def _preload_cuda_libraries() -> None:
    """pip로 설치한 cuBLAS와 cuDNN을 먼저 올린다.

    ctranslate2는 이 라이브러리를 시스템 경로에서 찾는데, WSL2에는 보통 없다.
    LD_LIBRARY_PATH를 설정하는 대신 여기서 직접 열어 두면 같은 프로세스에서 찾아 쓴다.
    """
    import nvidia

    base = Path(next(iter(nvidia.__path__)))
    # cuDNN의 하위 라이브러리는 추론 중에 이름으로 열리므로 전부 미리 올린다.
    for pattern in ("cublas/lib/libcublasLt.so.*", "cublas/lib/libcublas.so.*", "cudnn/lib/libcudnn*.so.*"):
        for lib in sorted(base.glob(pattern)):
            ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)


def _load_on(device: str):
    from faster_whisper import WhisperModel

    if device == "cuda":
        _preload_cuda_libraries()
    compute_type = "float16" if device == "cuda" else "int8"
    model = WhisperModel(config.whisper_model(), device=device, compute_type=compute_type)
    # cuDNN 문제는 모델을 올릴 때가 아니라 첫 추론에서 드러난다. 무음으로 한 번 돌려 본다.
    segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="ko")
    list(segments)
    return model


def load() -> None:
    """모델을 올린다. 여러 번 불러도 한 번만 올라간다."""
    global _model, _device, _load_error
    with _lock:
        if _model is not None:
            return
        wanted = config.whisper_device()
        candidates = ["cuda", "cpu"] if wanted == "auto" else [wanted]
        errors = []
        for device in candidates:
            started = time.monotonic()
            try:
                _model = _load_on(device)
            except Exception as e:  # 장치별 실패 원인이 다양해 넓게 잡고 다음 장치로 넘어간다
                errors.append(f"{device}: {e}")
                log.warning("whisper load failed on %s: %s", device, e)
                continue
            _device = device
            _load_error = None
            log.info(
                "whisper loaded model=%s device=%s %.1fs",
                config.whisper_model(), device, time.monotonic() - started,
            )
            return
        _load_error = " / ".join(errors)
        raise STTError(f"음성 인식 모델을 올리지 못했습니다. {_load_error}")


def load_in_background() -> None:
    """서버 시작 때 미리 올려 첫 답변이 모델 로딩을 기다리지 않게 한다."""

    def run():
        try:
            load()
        except STTError:
            pass  # 원인은 status()로 보여준다

    threading.Thread(target=run, name="whisper-load", daemon=True).start()


def status() -> dict:
    return {
        "model": config.whisper_model(),
        "device": _device or config.whisper_device(),
        "loaded": _model is not None,
        "error": _load_error,
    }


def transcribe(path: Path) -> Transcript:
    """음성 파일을 한국어 텍스트로 옮긴다. 말소리가 없으면 text가 빈 문자열."""
    load()
    started = time.monotonic()
    try:
        segments, info = _model.transcribe(
            str(path),
            language="ko",
            beam_size=5,
            # 무음 구간을 걸러 내 조용한 부분에서 없는 말을 지어내는 것을 줄인다.
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:
        log.warning("transcribe failed: %s", type(e).__name__)
        raise STTError("녹음을 읽지 못했습니다. 녹음이 비어 있거나 손상됐을 수 있습니다.") from e
    # 답변 내용은 찍지 않는다.
    log.info(
        "transcribe device=%s audio=%.1fs took=%.1fs chars=%d",
        _device, info.duration, time.monotonic() - started, len(text),
    )
    speech_sec = getattr(info, "duration_after_vad", None)
    return Transcript(
        text=text,
        audio_sec=float(info.duration),
        speech_sec=float(info.duration if speech_sec is None else speech_sec),
    )
