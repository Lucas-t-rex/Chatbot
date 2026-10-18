# Usar uma imagem oficial do Python como base
FROM python:3.10-slim

# Definir o diretório de trabalho dentro do contêiner
WORKDIR /app

# Copiar o arquivo de dependências para dentro do contêiner
COPY requirements.txt requirements.txt

# Instalar as dependências
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todo o resto do seu projeto para dentro do contêiner
COPY . .

# Porta usada pelo gunicorn (o fly.toml aponta para ela)
EXPOSE 8000

# Comando para executar sua aplicação quando o contêiner iniciar
# 1 worker SEMPRE: o agendador (resumos, follow-ups) roda dentro do processo.
# Com 2 workers cada mensagem agendada sairia duplicada.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "120", "main:app"]