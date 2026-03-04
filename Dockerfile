FROM python:3.11-slim

# 時區
ENV TZ=Asia/Taipei
RUN apt-get update && apt-get install -y tzdata && \
    ln -sf /usr/share/zoneinfo/Asia/Taipei /etc/localtime && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依賴先裝（利用 Docker layer cache）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
