# ------------------------------------------------------------
# Congyin V3 — Optimal Railway Dockerfile
# ------------------------------------------------------------

# 使用更輕量的 Python 3.11 正式版（Railway 支援最佳）
FROM python:3.11-slim

# 避免 Python 緩存產生大量不要的 cache 檔案
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 設定工作目錄
WORKDIR /app

# 安裝基本依賴（curl, build tools, SSL, etc）
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    libffi-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 將 requirements.txt 複製進容器並安裝
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# 複製 bot 程式碼
COPY . .

# Railway 的 Volume (/data) 將會自動掛載
# 無需在 Dockerfile 宣告 VOLUME，Railway 會接管

# 啟動 bot
CMD ["python", "bot.py"]
