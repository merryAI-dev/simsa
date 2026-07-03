"""VLM(Gemini) 응답 캐시 — 파일 SHA-256 + 프롬프트 버전 키.

프롬프트는 건드리지 않는다. 같은 파일 + 같은 프롬프트 버전이면 API 호출 없이
캐시된 응답을 돌려준다. 디버깅 중 같은 ZIP 재제출 시 비용이 0이 된다.

사용:
    from vlm_cache import VLMCache
    cache = VLMCache(Path("cache/vlm"), prompt_version="v1")

    key = cache.key_for(file_path)              # 파일 전체 기준
    key = cache.key_for(file_path, extra="p3")  # 페이지/청크 단위면 extra 로 구분

    cached = cache.get(key)
    if cached is None:
        response = call_gemini(...)             # 기존 호출 그대로
        cache.set(key, response, source=file_path.name)
    else:
        response = cached

프롬프트 문구를 바꾸면 prompt_version 을 올려야 한다. 안 올리면 옛 응답이 나온다.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


class VLMCache:
    def __init__(self, cache_dir: str | Path, prompt_version: str):
        self.cache_dir = Path(cache_dir)
        self.prompt_version = prompt_version
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def key_for(self, file_path: str | Path, extra: str = "") -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        h.update(self.prompt_version.encode())
        if extra:
            h.update(extra.encode())
        return h.hexdigest()

    def get(self, key: str):
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        except (json.JSONDecodeError, KeyError, OSError):
            return None  # 손상된 캐시는 miss 처리

    def set(self, key: str, response, source: str = "") -> None:
        payload = json.dumps(
            {"prompt_version": self.prompt_version, "source": source, "response": response},
            ensure_ascii=False,
        )
        # 원자적 쓰기: 임시 파일 → rename (중단돼도 반쪽 캐시가 안 남음)
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self._path(key))
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"
