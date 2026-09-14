import os
import time
import psycopg2
from flask import Flask

app = Flask(__name__)

def get_db_connection():
    retries = 5
    while retries > 0:
        try:
            conn = psycopg2.connect(
                host=os.environ.get('DB_HOST', 'db'),
                database=os.environ.get('POSTGRES_DB', 'myapp'),
                user=os.environ.get('POSTGRES_USER', 'user'),
                password=os.environ.get('POSTGRES_PASSWORD', 'password')
            )
            return conn
        except psycopg2.OperationalError:
            retries -= 1
            print("База еще не готова, ждем 2 секунды...")
            time.sleep(2)
    raise Exception("Не удалось подключиться к базе данных")

@app.route('/')
def index():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('CREATE TABLE IF NOT EXISTS visits (id SERIAL PRIMARY KEY, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP);')
    cur.execute('INSERT INTO visits DEFAULT VALUES;')
    conn.commit()
    
    cur.execute('SELECT COUNT(*) FROM visits;')
    count = cur.fetchone()[0]
    
    cur.close()
    conn.close()
    
    return f"<h1>Привет! Это твое приложение на Python + PostgreSQL!</h1><h3>Количество визитов: {count}</h3>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
