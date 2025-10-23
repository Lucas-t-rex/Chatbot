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

# Comando para executar sua aplicação quando o contêiner iniciar
CMD ["python", "main.py"]