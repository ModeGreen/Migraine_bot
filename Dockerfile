FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    QUESTIONS_FILE=/app/questions.yaml

WORKDIR /app
COPY requirements.txt .
# /etc/localtime удаляем, чтобы на его место смонтировался файл хоста (часовой пояс сервера)
RUN pip install -r requirements.txt && rm -f /etc/localtime

COPY bot ./bot
COPY questions.yaml .

CMD ["python", "-m", "bot.main"]
