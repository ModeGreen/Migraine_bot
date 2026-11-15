"""Расшифровка голосовых через faster-whisper.

Модель загружается только при первом голосовом и выгружается после
WHISPER_UNLOAD_MINUTES минут простоя, так что в остальное время
память не занята.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model_name: str, models_dir: Path, language: str, unload_minutes: int):
        self.model_name = model_name
        self.models_dir = models_dir
        self.language = language or None
        self.unload_after = unload_minutes * 60
        self._model = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.model_name.lower() not in ("", "off", "none", "false", "0")

    def _load(self):
        from faster_whisper import WhisperModel

        log.info("Загружаю модель Whisper %s…", self.model_name)
        started = time.monotonic()
        model = WhisperModel(
            self.model_name,
            device="cpu",
            compute_type="int8",
            download_root=str(self.models_dir),
        )
        log.info("Модель загружена за %.1f с", time.monotonic() - started)
        return model

    def _run(self, path: Path) -> str:
        if self._model is None:
            self._model = self._load()
        segments, _info = self._model.transcribe(
            str(path), language=self.language, beam_size=5, vad_filter=True
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, path: Path) -> str:
        """Расшифровывает файл. Голосовые обрабатываются по одному."""
        async with self._lock:
            started = time.monotonic()
            text = await asyncio.to_thread(self._run, path)
            self._last_used = time.monotonic()
            log.info("Расшифровано %s за %.1f с", path.name, self._last_used - started)
            return text

    async def unload_if_idle(self) -> None:
        if self._model is None or self._lock.locked():
            return
        if time.monotonic() - self._last_used < self.unload_after:
            return
        async with self._lock:
            self._model = None
            gc.collect()
            log.info("Модель Whisper выгружена из памяти (простой)")
