# Polymarket bot-v2 — runtime 24/7
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Deps del sistema mínimas para py_clob_client (compila algunas wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Instala dependencias Python
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# Copia solo bot-v2 (la versión activa)
COPY bot-v2/ /app/bot-v2/

WORKDIR /app/bot-v2

# Sin .env dentro del contenedor: todas las credenciales por env vars del servicio cloud.
# Railway/Fly inyectan WALLET_ADDRESS, PRIVATE_KEY, BASE44_API_KEY, etc. al runtime.

CMD ["python", "-u", "main.py"]
