FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sql ./sql
COPY evprices ./evprices

RUN useradd -r -u 1001 evprices
USER evprices

EXPOSE 8080
CMD ["uvicorn", "evprices.web.app:app", "--host", "0.0.0.0", "--port", "8080"]
