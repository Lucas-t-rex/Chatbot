# Dockerfile Definitivo e Correto (v6)

# 1. Imagem base do Python
FROM python:3.10-slim

# 2. Diretório de trabalho
WORKDIR /app

# 3. Instala ferramentas e a versão correta do Node.js
RUN apt-get update && apt-get install -y curl git && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g pm2

# 4. Copia e instala as dependências do Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Clona a API, instala, GERA O PRISMA (com o caminho certo) e constrói
RUN git clone https://github.com/EvolutionAPI/evolution-api.git evolution-api && \
    cd evolution-api && npm install && npx prisma generate --schema=./prisma/schema.prisma && npm run build

# 6. Copia o resto do seu código (main.py)
COPY . .

# 7. Expõe a porta do seu aplicativo
EXPOSE 8000

# 8. Comando final para iniciar os dois processos juntos
CMD ["/bin/bash", "-c", "pm2 start evolution-api/dist/index.js --name evolution-api && gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 120 main:app"]