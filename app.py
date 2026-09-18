from flask import Flask, request, redirect, url_for

app = Flask(__name__)

# 1. Наша импровизированная "база данных"
# В реальном проекте тут подключение к PostgreSQL, но для теста 
# обновления интерфейса мы сохраним данные прямо в память (словарь).
inventory = {
    "Сила": 5,
    "Магия": 2,
    "Долбоебизм": 10
}

# 2. Главная страница (Лицевая часть / Фронтенд)
@app.route('/')
def index():
    # Пишем HTML-код прямо внутри Python (так делают для простых проектов)
    html = """
    <html>
    <head>
        <title>Интерактивная таблица</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background-color: #f4f4f9; }
            table { border-collapse: collapse; width: 60%; background: white; margin-top: 20px; }
            th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
            th { background-color: #2c3e50; color: white; }
            button { padding: 8px 15px; background: #27ae60; color: white; border: none; cursor: pointer; border-radius: 4px;}
            button:hover { background: #219150; }
        </style>
    </head>
    <body>
        <h2>Панель управления ресурсами (Версия 1.0)</h2>
        <table>
            <tr>
                <th>Имя ресурса</th>
                <th>Количество (шт)</th>
                <th>Действие</th>
            </tr>
    """
    
    # 3. Динамически строим строки таблицы на основе нашей "базы"
    for item, count in inventory.items():
        html += f"""
            <tr>
                <td>{item}</td>
                <td>{count}</td>
                <td>
                    <!-- Форма, которая отправляет команду на сервер -->
                    <form action="/add/{item}" method="POST" style="margin:0;">
                        <button type="submit">+ Добавить</button>
                    </form>
                </td>
            </tr>
        """
        
    html += """
        </table>
    </body>
    </html>
    """
    return html

# 4. Логика обработки кнопок (Бэкенд)
@app.route('/add/<item_name>', methods=['POST'])
def add_item(item_name):
    # Если нажали кнопку, берем имя элемента и увеличиваем цифру на 1
    if item_name in inventory:
        inventory[item_name] += 1
    
    # После плюсования - мгновенно возвращаем пользователя на главную страницу
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Запускаем веб-сервер на стандартном порту 5000
    app.run(host='0.0.0.0', port=5000)