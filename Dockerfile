FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/data/model-cache
WORKDIR /app
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements-ml.txt
COPY . .
RUN chmod -R a+rX /app \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/uploads /data/model-cache \
    && chown -R appuser:appuser /data
USER appuser
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","1"]
