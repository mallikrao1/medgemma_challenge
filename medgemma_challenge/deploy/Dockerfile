FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY medgemma_challenge/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY medgemma_challenge /app/medgemma_challenge

ENV MODEL_BACKEND=mock
ENV API_PORT=8010

EXPOSE 8010

CMD ["uvicorn", "medgemma_challenge.app.main:app", "--host", "0.0.0.0", "--port", "8010"]

