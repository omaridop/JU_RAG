FROM python:3.13-slim

WORKDIR /app

# build-essential covers any sdist-only deps (chromadb / PyMuPDF ship wheels,
# but this keeps the build reliable); curl is used by the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY app ./app

# data/ (SQLite + Chroma + uploads) is bind-mounted at runtime, not baked in.
EXPOSE 8000

# Production WSGI server. run:app is the Flask app built by create_app().
CMD ["waitress-serve", "--listen=0.0.0.0:8000", "--threads=6", "run:app"]
