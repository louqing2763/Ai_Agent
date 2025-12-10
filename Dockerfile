FROM python:3.10-slim

# 讓印出的 log 可以即時看到（避免 Starting Container 卡住）
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 安裝必要依賴（適用 telegram-bot job_queue / aiohttp 等）
RUN apt-get update && apt-get install -y \
    libcurl4-openssl-dev \
    libssl-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 先複製 requirements（Docker caching 比較好）
COPY requirements.txt .

# 安裝 Python 套件
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 再複製程式碼
COPY . .

CMD ["python", "main.py"]
