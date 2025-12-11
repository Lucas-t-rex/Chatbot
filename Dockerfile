# Usar uma imagem oficial do Python como base
FROM python:3.10-slim

# Definir o diretório de trabalho
WORKDIR /app

# 1. Copia e instala dependências (Isso o Docker faz cache, fica rápido)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. FORÇA BRUTA: Copia explicitamente a lista antes de copiar o resto
# Se o arquivo não estiver na pasta, o deploy vai falhar AQUI e te avisar
COPY lista.xlsx .

# 3. Copia o restante do código (main.py, etc)
COPY . .

# Informa porta
EXPOSE 8000

# Comando para rodar (ajustado para variável de porta do Fly ou padrão 8000)
# O ${PORT:-8000} garante que use a porta que o Fly mandar
CMD gunicorn --bind 0.0.0.0:8000 --workers 1 --timeout 120 main:app