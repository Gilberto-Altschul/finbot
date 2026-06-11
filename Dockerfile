# Usa uma versão oficial e leve do Python
FROM python:3.11-slim

# Define o diretório de trabalho dentro do container
WORKDIR /app

# Instala dependências do sistema necessárias para leitura de PDFs ou libs comuns
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copia o ficheiro de requisitos primeiro (isso acelera os builds futuros)
COPY requirements.txt .

# Instala as bibliotecas Python
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o teu código para dentro do container
COPY . .

# Expõe a porta que o Railway vai usar
EXPOSE 8080

# Adiciona a pasta atual ao PYTHONPATH
ENV PYTHONPATH=/app

# Inicia com uvicorn na porta injetada pelo Railway
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]