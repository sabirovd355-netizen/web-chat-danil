import eventlet
eventlet.monkey_patch() # !!! ОЧЕНЬ ВАЖНО: Асинхронный патч должен быть первым импортом !!!

import os
import json
import secrets
from datetime import datetime, timezone

from flask import Flask, render_template, redirect, url_for, request, session, abort, flash
# ВАЖНО: 'rooms' теперь не используется в connect
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

from oauthlib.oauth2 import WebApplicationClient
import requests

# --- 1. Конфигурация и Инициализация ---
app = Flask(__name__)

# !!! ИСПРАВЛЕНИЕ ДЛЯ RAILWAY (HTTPS) !!!
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Устанавливаем секретный ключ для защиты сессий
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", secrets.token_hex(16))

# !!! НАСТРОЙКА БАЗЫ ДАННЫХ !!!
database_url = os.getenv("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    # Исправляем формат URL для совместимости с SQLAlchemy и PostgreSQL
    database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
# Используем eventlet для SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- 2. Настройки OAuth (Google) ---
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

client = WebApplicationClient(GOOGLE_CLIENT_ID)

# --- 3. Модели Базы Данных ---
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String(128), primary_key=True) # ID пользователя Google (sub)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    profile_pic = db.Column(db.String(256))

class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    room_name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.String(128), db.ForeignKey('users.id'), nullable=False)
    user_name = db.Column(db.String(100), nullable=False)
    user_pic = db.Column(db.String(256))
    content = db.Column(db.String(500), nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        """Возвращает сообщение в виде словаря для отправки через SocketIO."""
        # Форматирование времени в соответствии с часовым поясом UTC
        # Если вы хотите локальное время, нужно добавить логику конвертации.
        # Для простоты оставляем UTC-время в формате ЧЧ:ММ
        return {
            'user_name': self.user_name,
            'user_pic': self.user_pic,
            'content': self.content,
            'timestamp': self.timestamp.strftime('%H:%M'), 
        }

# --- 4. Вспомогательные функции (OAuth) ---

def get_google_provider_cfg():
    """Получает конфигурацию Google OpenID Connect."""
    return requests.get(GOOGLE_DISCOVERY_URL).json()

# --- 5. Маршруты Аутентификации (Flask) ---

@app.route("/")
def index():
    """Главная страница: перенаправляет, если пользователь уже вошел."""
    # Если пользователь авторизован, отправляем его на выбор комнаты
    if 'user_id' in session:
        return redirect(url_for("room_choice"))
    # Иначе, показываем страницу входа (требуется шаблон login.html)
    return render_template("login.html")

@app.route("/google-login")
def google_login():
    """Инициирует процесс входа через Google OAuth."""
    google_provider_cfg = get_google_provider_cfg()
    authorization_endpoint = google_provider_cfg["authorization_endpoint"]

    # Используем клиент для создания запроса
    redirect_uri = request.base_url + "/callback"
    
    # ПРОВЕРКА: Если приложение работает на http, request.base_url будет http://.
    # Для Railway или других HTTPS-хостов это может вызвать проблему, но ProxyFix
    # должен исправить request.base_url в большинстве случаев.

    request_uri = client.prepare_request_uri(
        authorization_endpoint,
        redirect_uri=redirect_uri,
        scope=["openid", "email", "profile"],
    )
    return redirect(request_uri)

@app.route("/google-login/callback")
def callback():
    """Обрабатывает ответ от Google и авторизует пользователя."""
    code = request.args.get("code")

    google_provider_cfg = get_google_provider_cfg()
    token_endpoint = google_provider_cfg["token_endpoint"]

    # Подготавливаем запрос на токен
    token_url, headers, body = client.prepare_token_request(
        token_endpoint,
        authorization_response=request.url,
        redirect_url=request.base_url,
        code=code
    )
    token_response = requests.post(
        token_url,
        headers=headers,
        data=body,
        auth=(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET),
    )

    client.parse_request_body_response(token_response.text)

    # Получаем конечную точку информации о пользователе
    userinfo_endpoint = google_provider_cfg["userinfo_endpoint"]
    uri, headers, body = client.add_token(userinfo_endpoint)
    userinfo_response = requests.get(uri, headers=headers, data=body)

    # Парсим информацию
    if userinfo_response.json().get("email_verified"):
        user_data = userinfo_response.json()
        
        user_id = user_data["sub"]
        user_name = user_data["name"]
        user_email = user_data["email"]
        user_pic = user_data["picture"]

        # Создаем или обновляем пользователя в БД
        user = User.query.get(user_id)
        if not user:
            user = User(id=user_id, name=user_name, email=user_email, profile_pic=user_pic)
            db.session.add(user)
        else:
            user.name = user_name
            user.profile_pic = user_pic
        
        db.session.commit()

        # Устанавливаем сессию
        session['user_id'] = user_id
        session['user_name'] = user_name
        session['user_pic'] = user_pic
        session['user_email'] = user_email # Добавляем почту для logout
        
        flash(f"Добро пожаловать, {user_name}!", 'success')
        return redirect(url_for("room_choice"))

    else:
        flash("Почта Google не подтверждена или отсутствует.", 'error')
        return redirect(url_for("index"))

@app.route("/logout")
def logout():
    """Очищает сессию и перенаправляет на страницу входа."""
    session.clear()
    flash("Вы вышли из системы.", 'success')
    return redirect(url_for("index"))

# --- 6. Маршруты Приложения (Flask) ---

@app.route("/room_choice", methods=["GET", "POST"])
def room_choice():
    """Страница выбора комнаты."""
    if 'user_id' not in session:
        flash("Пожалуйста, войдите, чтобы продолжить.", 'error')
        return redirect(url_for("index"))

    room_error = None
    if request.method == "POST":
        room_name = request.form.get("room_name", "").strip()
        if room_name:
            # Перенаправляем на страницу чата с выбранным именем комнаты
            return redirect(url_for("chat", room_name=room_name))
        else:
            room_error = "Имя комнаты не может быть пустым."
            
    # Переменные для шаблона room_choice.html
    user_name = session.get('user_name', 'Гость')
    user_email = session.get('user_email', 'неизвестно')
    
    # Заглушки для популярных комнат
    popular_rooms = [
        "Общий Флуд", 
        "Кодеры", 
        "Игры и Развлечения", 
        "Flask-SocketIO",
        "Спорт"
    ]
            
    return render_template("room_choice.html", 
                           popular_rooms=popular_rooms, 
                           room_error=room_error,
                           user_name=user_name,
                           user_email=user_email)

@app.route("/chat/<room_name>")
def chat(room_name):
    """Страница чата для конкретной комнаты."""
    if 'user_id' not in session:
        flash("Пожалуйста, войдите, чтобы продолжить.", 'error')
        return redirect(url_for("index"))

    # Нормализация имени комнаты
    room_name = room_name.strip()
    if not room_name:
        flash("Недопустимое имя комнаты.", 'error')
        return redirect(url_for("room_choice"))
    
    user_name = session.get('user_name')
    user_pic = session.get('user_pic')

    # Загружаем последние 50 сообщений для этой комнаты
    history_messages = Message.query.filter_by(room_name=room_name) \
                                   .order_by(Message.timestamp.desc()) \
                                   .limit(50) \
                                   .all()
    # Разворачиваем список, чтобы старые сообщения были в начале
    history_messages.reverse()
    
    # Преобразуем объекты в словари для шаблона
    messages_data = [msg.to_dict() for msg in history_messages]

    return render_template("chat.html", 
                           room_name=room_name,
                           user_name=user_name,
                           user_pic=user_pic,
                           messages=messages_data)

# --- 7. Структура SocketIO ---

# Карта для отслеживания, в какой комнате находится каждый пользователь (sid -> room_name)
# Используем sid, так как это уникально для каждого SocketIO-соединения
sid_room_map = {} 

@socketio.on('connect')
def handle_connect():
    """Обрабатывает подключение нового клиента (только проверка авторизации)."""
    user_id = session.get("user_id")
    # Проверяем, авторизован ли пользователь в Flask-сессии
    if not user_id:
        print("Неавторизованный клиент попытался подключиться.")
        disconnect()
        return

    # Если авторизован, ждем, пока клиент отправит событие 'join_room'
    print(f'Пользователь {session["user_name"]} (SID: {request.sid}) подключен.')

@socketio.on('join_room')
def handle_join_room(data):
    """Явно присоединяет клиента к комнате."""
    user_id = session.get("user_id")
    room_name = data.get('room')
    
    if not all([user_id, room_name]):
        return 

    # 1. Если пользователь уже в другой комнате, выходим из нее
    current_sid = request.sid
    if current_sid in sid_room_map:
        old_room = sid_room_map[current_sid]
        if old_room != room_name:
            leave_room(old_room)
            print(f'{session["user_name"]} покинул комнату {old_room}.')

    # 2. Присоединяемся к новой комнате
    join_room(room_name)
    sid_room_map[current_sid] = room_name

    # 3. Сообщаем всем, что пользователь присоединился
    emit('status_message', 
          {'msg': f'{session["user_name"]} присоединился к комнате {room_name}.'}, 
          room=room_name)
    
    print(f'Пользователь {session["user_name"]} (SID: {current_sid}) присоединен к комнате {room_name}.')


@socketio.on('disconnect')
def handle_disconnect():
    """Обрабатывает отключение пользователя."""
    user_id = session.get("user_id")
    current_sid = request.sid
    
    if current_sid in sid_room_map:
        room_name = sid_room_map.pop(current_sid)
        
        # Сообщаем всем, что пользователь отключился
        emit('status_message', 
              {'msg': f'{session["user_name"]} покинул комнату {room_name}.'}, 
              room=room_name)
        
        print(f'Пользователь {session["user_name"]} отключен от комнаты {room_name}.')

@socketio.on('send_message')
def handle_message(data):
    """Обрабатывает отправку нового сообщения."""
    user_id = session.get("user_id")
    current_sid = request.sid
    room_name = sid_room_map.get(current_sid)
    content = data.get('content', '').strip()

    if not all([user_id, room_name, content]):
        return # Игнорируем неполные или пустые сообщения

    if len(content) > 500: # Ограничение на размер сообщения
        content = content[:500] 

    # 1. Сохраняем сообщение в базу данных
    user = User.query.get(user_id)
    if not user:
        return

    new_message = Message(
        room_name=room_name,
        user_id=user.id,
        user_name=user.name,
        user_pic=user.profile_pic,
        content=content
    )
    db.session.add(new_message)
    db.session.commit()

    # 2. Отправляем сообщение всем в комнате
    message_data = new_message.to_dict()
    emit('new_message', message_data, room=room_name)
    print(f'Сообщение в комнате {room_name} от {user.name}: {content}')


# --- 8. Инициализация БД ---

with app.app_context():
    # Создаем таблицы, если они еще не существуют
    try:
        db.create_all()
        print("Проверка/создание таблиц базы данных завершена.")
    except Exception as e:
        print(f"Ошибка при создании таблиц базы данных: {e}")

# --- 9. Запуск Приложения ---

# Запуск приложения через gunicorn / eventlet (как указано в Procfile)
# if __name__ == '__main__':
#     socketio.run(app, host='0.0.0.0', port=5000, debug=True)