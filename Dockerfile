FROM python:3.10-slim
WORKDIR /app
RUN pip install --no-cache-dir flask psycopg2-binary gunicorn
COPY . .
RUN mkdir -p /app/uploads
EXPOSE 5000
# 1 воркер + потоки: init_db() выполняется при импорте, несколько воркеров дрались бы за создание таблиц
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "app:app"]
