FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reaper/ reaper/
COPY main.py .
COPY data/contracts/ data/contracts/

ENV PORT=8080
CMD exec uvicorn main:api --host 0.0.0.0 --port ${PORT}
