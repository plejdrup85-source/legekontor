# Bruk offisiell Python image
FROM python:3.11-slim

# Unngå buffering i logs
ENV PYTHONUNBUFFERED=1

# Installer systemavhengigheter
RUN apt-get update && apt-get install -y \
    build-essential \
    tesseract-ocr \
    tesseract-ocr-nor \
    tesseract-ocr-eng \
    poppler-utils \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Sett arbeidsmappe
WORKDIR /app

# Kopier requirements først (for caching)
COPY requirements.txt .

# Installer Python-avhengigheter
RUN pip install --no-cache-dir -r requirements.txt

# Kopier resten av applikasjonen
COPY . .

# Eksponer port (Render bruker 10000)
EXPOSE 10000

# Start FastAPI med uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
