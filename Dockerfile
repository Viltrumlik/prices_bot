FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

ENV PYTHONUNBUFFERED=1

# The bot writes nothing and needs no privileges.
RUN useradd --create-home --uid 10001 bot
USER bot

CMD ["python", "-u", "bot.py"]
