# Dockerfile Definitivo v7 (com o caminho do Prisma 100% correto)

# 1. Começamos com a imagem base do Python
FROM python:3.10-slim

# 2. Define o diretório de trabalho
WORKDIR /app

# 3. Instala as ferramentas e a versão correta do Node.js (v22)
RUN apt-get update && apt-get install -y curl git && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g pm2

# 4. Copia e instala as dependências do seu Chatbot Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Clona, instala, GERA O PRISMA com o caminho CORRETO e constrói a API
#    A MUDANÇA ESTÁ AQUI: --schema=./schema.prisma (sem a pasta "prisma/")
RUN git clone https://github.com/EvolutionAPI/evolution-api.git evolution-api && \
    cd evolution-api && npm install && npx prisma generate --schema=./schema.prisma && npm run build

# 6. Copia o resto do seu código (main.py, etc)
COPY . .

# 7. Informa ao Koyeb a porta que sua aplicação usa
EXPOSE 8000

# 8. Comando final para iniciar os dois processos
CMD ["/bin/bash", "-c", "pm2 start evolution-api/dist/index.js --name evolution-api && gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 120 main:app"]

