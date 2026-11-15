FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    DATA_DIR=/app/data \
    QUESTIONS_FILE=/app/questions.yaml \
    HF_HOME=/app/data/hf

WORKDIR /app
COPY requirements.txt .
# /etc/localtime удаляем, чтобы на его место смонтировался файл хоста (пояс по умолчанию)
RUN pip install -r requirements.txt \
    && rm -f /etc/localtime \
    && useradd --create-home --uid 1000 app

COPY bot ./bot
COPY questions.yaml .

# Бот работает не от root
USER app

CMD ["python", "-m", "bot.main"]
