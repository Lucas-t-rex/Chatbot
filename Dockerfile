# Dockerfile Definitivo e Correto (v13 - Abordagem Multi-Stage)

# --- ESTÁGIO 1: O CONSTRUTOR (BUILDER) ---
# Usamos uma imagem Node.js dedicada apenas para compilar a Evolution API.
FROM node:20-slim as builder

WORKDIR /build

# Instala apenas o Git, necessário para clonar o repositório
RUN apt-get update && apt-get install -y git

# Clona, instala dependências e COMPILA a API
# A variável DATABASE_URL é necessária para o build funcionar
RUN git clone https://github.com/EvolutionAPI/evolution-api.git . && \
    export DATABASE_URL="postgresql://user:pass@localhost:5432/db" && \
    npm install && \
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

# A MÁGICA ACONTECE AQUI!
# Copiamos APENAS a pasta 'dist' compilada do Estágio 1 (builder)
COPY --from=builder /build/dist ./evolution-api/dist
# Copiamos também o package.json para que o PM2 funcione corretamente
COPY --from=builder /build/package.json ./evolution-api/package.json

# Copia o resto do seu código (main.py)
COPY . .

# Expõe a porta do seu aplicativo
EXPOSE 8000

# Comando final para iniciar os dois processos juntos
CMD ["/bin/bash", "-c", "pm2 start evolution-api/dist/index.js --name evolution-api && gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 120 main:app"]