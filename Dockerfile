# Step 1: Use optimized python runtime image base
FROM python:3.11-slim

# Step 2: Prevent python from writing pyc and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Step 3: Set absolute workspace path inside container
WORKDIR /app

# Step 4: Install system dependencies for fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Step 5: Ingest dependencies layer
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Step 6: Ingest all project source codes into /app root directly
COPY . .

# Step 7: Launch main entry point directly from workspace root
CMD ["python", "main.py"]