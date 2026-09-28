FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home tablekeeper
WORKDIR /service
COPY app ./app
COPY tests ./tests
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=2s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=2).read()"
CMD ["python", "-m", "app"]
