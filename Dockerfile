# Dockerfile Definitivo (Python + Node.js)

# 1. Começamos com a imagem base do Python
FROM python:3.10-slim

# 2. Define o diretório de trabalho
WORKDIR /app

# 3. Instala as ferramentas necessárias para baixar Node.js e Git
RUN apt-get update && apt-get install -y curl git && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g pm2

# 4. Copia e instala as dependências do seu Chatbot Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Clona a Evolution API e instala as dependências dela
RUN git clone https://github.com/EvolutionAPI/evolution-api.git evolution-api && \
    cd evolution-api && npm install --only=production --ignore-scripts

# 6. Copia o resto do seu código (main.py, etc)
COPY . .

# 7. Informa ao Koyeb a porta que sua aplicação usa
EXPOSE 8000

# 8. Comando final para iniciar os dois processos
#    - Inicia a Evolution API em segundo plano usando o 'pm2'
#    - Inicia seu chatbot Python em primeiro plano usando o 'gunicorn'
CMD ["/bin/bash", "-c", "pm2 start evolution-api/dist/src/server.js --name evolution-api && gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 120 main:app"]