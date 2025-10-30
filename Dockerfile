
FROM python:3.10-slim

# Define o diretório de trabalho
WORKDIR /app

# Instala Node.js, Git e dependências básicas
RUN apt-get update && apt-get install -y curl git build-essential ffmpeg && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g pm2

# Copia e instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Clona Evolution API e instala dependências
RUN git clone https://github.com/EvolutionAPI/evolution-api.git evolution-api && \
    cd evolution-api && npm install

# Copia o código do Flask
COPY . .

# Expõe a porta principal
EXPOSE 8000

# Inicia Evolution API e Flask juntos
CMD ["/bin/bash", "-c", "\
    pm2 start evolution-api/dist/index.js --name evolution-api && \
    gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 120 main:app \
"]
