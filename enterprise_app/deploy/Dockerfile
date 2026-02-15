FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY enterprise_app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY enterprise_app /app/enterprise_app

EXPOSE 8020

CMD ["uvicorn", "enterprise_app.app.main:app", "--host", "0.0.0.0", "--port", "8020"]

