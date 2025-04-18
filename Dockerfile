﻿FROM python:3.10-slim
WORKDIR /app
COPY grid.py .
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "grid.py"]