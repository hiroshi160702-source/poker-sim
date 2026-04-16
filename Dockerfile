FROM python:3.11-slim

# Render などのホスティング環境はこのコンテナイメージからアプリを起動します。
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000

# ホスティング側が渡すポートを使い、FastAPI を外部公開向けに待ち受けます。
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
