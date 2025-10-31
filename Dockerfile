# Dockerfile Definitivo (v16 - A Versão Final de Verdade)

# --- ESTÁGIO 1: O CONSTRUTOR (BUILDER) ---
# Usamos uma imagem Node.js dedicada apenas para compilar a Evolution API.
FROM node:20-slim as builder

WORKDIR /build

# Instala apenas o Git, necessário para clonar o repositório
RUN apt-get update && apt-get install -y git

# A SOLUÇÃO FINAL ESTÁ AQUI:
# Adicionamos '--ignore-scripts' para pular a execução do 'husky'.
RUN export NODE_OPTIONS="--max-old-space-size=200" && \
    git clone https://github.com/EvolutionAPI/evolution-api.git . && \
    export DATABASE_URL="postgresql://user:pass@localhost:5432/db" && \
    npm install --omit=dev --ignore-scripts && \
    npm run build


# --- ESTÁGIO 2: A IMAGEM FINAL ---
# Começamos do zero com a imagem Python leve.
FROM python:3.10-slim

WORKDIR /app

# Instala as ferramentas necessárias (Node.js e PM2) para RODAR a API, não para construir
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g pm2

# Copia e instala as dependências do seu Chatbot Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos a pasta 'dist' e o 'package.json' já prontos do Estágio 1 (builder)
COPY --from=builder /build/dist ./evolution-api/dist
COPY --from=builder /build/package.json ./evolution-api/package.json

# Copia o resto do seu código (main.py)
COPY . .

# Expõe a porta do seu aplicativo
EXPOSE 8000

# Comando final para iniciar os dois processos juntos
CMD ["/bin/bash", "-c", "pm2 start evolution-api/dist/index.js --name evolution-api && gunicorn --bind 0.0.0.0:8000 --workers 1 --timeout 120 main:app"]