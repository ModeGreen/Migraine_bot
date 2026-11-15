"""Расшифровка голосовых через faster-whisper.

- Модель скачивается один раз в data/models/faster-whisper-<имя> фоном при старте бота,
  а не при первом голосовом. Пока она не готова, голосовые сохраняются и
  расшифровываются позже. Модель можно положить в эту папку и вручную (см. README).
- В память модель загружается только при расшифровке и выгружается после
  WHISPER_UNLOAD_MINUTES минут простоя.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
DOWNLOAD_RETRY_SECONDS = 600


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

    @property
    def model_dir(self) -> Path:
        custom = Path(self.model_name)
        if custom.is_absolute():
            return custom  # WHISPER_MODEL=/путь/к/папке/с/моделью
        return self.models_dir / f"faster-whisper-{self.model_name}"

    @property
    def ready(self) -> bool:
        """Все файлы модели на диске. huggingface_hub качает во временный *.incomplete
        и кладёт файл на место только целиком, так что недокачанный model.bin здесь не появится."""
        return all((self.model_dir / f).is_file() for f in MODEL_FILES)

    # ---------- скачивание ----------
    def _download(self) -> None:
        from faster_whisper import download_model

        download_model(self.model_name, output_dir=str(self.model_dir))

    async def download_forever(self) -> None:
        """Фоновая задача: докачивает модель, при ошибке пробует снова через 10 минут."""
        if not self.enabled:
            return
        while not self.ready:
            if Path(self.model_name).is_absolute():
                log.error("Модель не найдена в %s — нужны файлы %s", self.model_dir, ", ".join(MODEL_FILES))
                return
            log.info("Скачиваю модель Whisper %s в %s (один раз)…", self.model_name, self.model_dir)
            started = time.monotonic()
            try:
                await asyncio.to_thread(self._download)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось скачать модель (%s). Повтор через %d мин. "
                            "Если сеть не пускает к huggingface.co — положите модель вручную, см. README.",
                            exc, DOWNLOAD_RETRY_SECONDS // 60)
                await asyncio.sleep(DOWNLOAD_RETRY_SECONDS)
                continue
            log.info("Модель скачана за %.0f с", time.monotonic() - started)
        log.info("Модель Whisper готова: %s", self.model_dir)

    # ---------- расшифровка ----------
    def _run(self, path: Path) -> str:
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("Загружаю модель Whisper в память…")
            started = time.monotonic()
            self._model = WhisperModel(str(self.model_dir), device="cpu", compute_type="int8")
            log.info("Модель загружена за %.1f с", time.monotonic() - started)
        segments, _info = self._model.transcribe(
            str(path), language=self.language, beam_size=5, vad_filter=True
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, path: Path) -> str:
        """Расшифровывает файл. Голосовые обрабатываются по одному. Требует ready=True."""
        if not self.ready:
            raise RuntimeError("Модель Whisper ещё не скачана")
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
