FROM python:3.12-slim

# Prevent Python from writing pyc files to disc
ENV PYTHONDONTWRITEBYTECODE=1
# Prevent Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (sqlite3 + gosu for unprivileged volume ownership drop)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Create data/config directories and non-root user (UID 1000)
RUN mkdir -p /app/data /app/config/logs && \
    useradd -u 1000 -m -s /bin/bash appuser && \
    chown -R appuser:appuser /app && \
    chmod -R 775 /app/config /app/data

# Expose port
EXPOSE 8000

# Ensure mounted volumes are owned by appuser and drop privileges via gosu
CMD ["sh", "-c", "mkdir -p /app/config/logs /app/data && chown -R appuser:appuser /app/config /app/data && exec gosu appuser uvicorn app.main:app --host 0.0.0.0 --port 8000"]
