import os
import json
import secrets
from datetime import datetime, timezone

from flask import Flask, render_template, redirect, url_for, request, session, abort, flash
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from flask_sqlalchemy import SQLAlchemy

from oauthlib.oauth2 import WebApplicationClient
import requests

# --- 1. Конфигурация и Инициализация ---
app = Flask(__name__)

# !!! ИСПРАВЛЕНИЕ ДЛЯ RAILWAY (HTTPS) !!!
# Говорим Flask, что мы находимся за прокси-сервером, и нужно использовать HTTPS.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Устанавливаем секретный ключ для защиты сессий
# Используем переменную окружения SECRET_KEY или генерируем случайный для локального запуска
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", secrets.token_hex(16))

# !!! НАСТРОЙКА БАЗЫ ДАННЫХ !!!
# Используем DATABASE_URL для PostgreSQL на Railway или fallback к SQLite для локальной разработки
database_url = os.getenv("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    # SQLAlchemy с psycopg2 требует "postgresql+psycopg2://"
    # Исправляем формат URL для совместимости с SQLAlchemy и PostgreSQL
    database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app)

# Конфигурация Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:5000/google/callback")
# Разрешаем HTTP для локальной разработки, если не установлены переменные окружения
if not GOOGLE_CLIENT_ID:
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

client = WebApplicationClient(GOOGLE_CLIENT_ID)

# Глобальная карта: Хранит, в какой комнате находится пользователь (по ID)
user_room_map = {} 

# --- 2. Модели и База данных ---

class User(db.Model):
    """Модель пользователя."""
    __tablename__ = 'users'
    id = db.Column(db.String(128), primary_key=True) # ID от Google
    name = db.Column(db.String(80), nullable=False)
    profile_pic = db.Column(db.String(255))
    email = db.Column(db.String(120), unique=True, nullable=False)
    
    def get_id(self):
        # Метод для получения ID, используемый в сессии
        return str(self.id)

class Message(db.Model):
    """Модель сообщения для истории."""
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.String(128), db.ForeignKey('users.id'), nullable=False)
    sender_name = db.Column(db.String(80), nullable=False)
    text = db.Column(db.String(500), nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=lambda: datetime.now(timezone.utc))
    room_code = db.Column(db.String(50), nullable=False) 

# --- 3. Функции Аутентификации и Пользователей ---

def get_google_provider_cfg():
    """Получает конфигурацию Google API."""
    return requests.get(GOOGLE_DISCOVERY_URL).json()

def add_or_update_user(user_info):
    """Создает или обновляет пользователя в базе данных."""
    with app.app_context():
        user = User.query.filter_by(id=user_info['sub']).first()
        if user is None:
            user = User(
                id=user_info['sub'],
                name=user_info['name'],
                email=user_info['email'],
                profile_pic=user_info.get('picture')
            )
            db.session.add(user)
        else:
            user.name = user_info['name']
            user.email = user_info['email']
            user.profile_pic = user_info.get('picture')
        
        db.session.commit()
        return user

# --- 4. Маршруты Аутентификации GOOGLE OAUTH (С ручным управлением сессией) ---

@app.route('/login')
def login():
    """Инициирует процесс входа через Google."""
    # Проверка, авторизован ли пользователь через сессию
    if session.get("user_id"):
        return redirect(url_for("choose_room"))
        
    google_provider_cfg = get_google_provider_cfg()
    authorization_endpoint = google_provider_cfg["authorization_endpoint"]

    request_uri = client.prepare_request_uri(
        authorization_endpoint,
        redirect_uri=GOOGLE_REDIRECT_URI,
        scope=["openid", "email", "profile"],
    )
    return redirect(request_uri)

@app.route("/google/callback")
def callback():
    """Обрабатывает ответ от Google и входит в систему."""
    code = request.args.get("code")
    
    google_provider_cfg = get_google_provider_cfg()
    token_endpoint = google_provider_cfg["token_endpoint"]

    # Обмениваем код на токен
    token_url, headers, body = client.prepare_token_request(
        token_endpoint,
        authorization_response=request.url,
        redirect_url=GOOGLE_REDIRECT_URI,
        code=code
    )
    token_response = requests.post(
        token_url,
        headers=headers,
        data=body,
        auth=(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    ).json()

    # Парсим ответ
    client.parse_request_body_response(json.dumps(token_response))

    # Получаем информацию о пользователе
    userinfo_endpoint = google_provider_cfg["userinfo_endpoint"]
    uri, headers, body = client.add_token(userinfo_endpoint)
    userinfo_response = requests.get(uri, headers=headers, data=body).json()

    # Проверяем, есть ли у пользователя email
    if not userinfo_response.get("email_verified"):
        flash("Пользователь не подтвердил email в Google.", "danger")
        return redirect(url_for("login"))

    # Создаем или загружаем пользователя
    user = add_or_update_user(userinfo_response)
    
    # Устанавливаем сессию вручную
    session["user_id"] = user.id
    session["name"] = user.name
    session["profile_pic"] = user.profile_pic

    # Перенаправляем на выбор комнаты
    return redirect(url_for("choose_room"))

@app.route('/logout')
def logout():
    """Выход пользователя (ручная очистка сессии)."""
    session.pop("user_id", None)
    session.pop("name", None)
    session.pop("profile_pic", None)
    session.pop("room_code", None)
    flash("Вы успешно вышли из системы.", "info")
    return redirect(url_for('login'))

# --- 5. МАРШРУТЫ ДЛЯ КОМНАТ ---

@app.route('/')
def choose_room():
    """Страница, где пользователь выбирает или создает комнату (room_choice.html)."""
    # Проверка авторизации через сессию 
    if not session.get("user_id"):
        return redirect(url_for("login"))
        
    return render_template('room_choice.html', username=session["name"]) 

@app.route('/chat/<room_code>', methods=["GET", "POST"])
def index(room_code):
    """Главная страница мессенджера (index.html) с кодом комнаты."""
    # Проверка авторизации через сессию 
    if not session.get("user_id"):
        return redirect(url_for("login"))
        
    # Устанавливаем код комнаты в сессии для SocketIO
    session["room_code"] = room_code

    # Загружаем историю только для этой комнаты
    history = Message.query.filter_by(room_code=room_code) \
                           .order_by(Message.timestamp.desc()).limit(50).all()
    history.reverse()
    
    # Передаем данные пользователя из сессии
    return render_template('index.html', 
                           username=session["name"], 
                           room_code=room_code,
                           history=history)

# --- 6. ОБРАБОТЧИКИ WebSockets ДЛЯ КОМНАТ (Проверки через сессию) ---

@socketio.on('join')
def on_join(data):
    """Пользователь присоединяется к комнате SocketIO."""
    # Получаем данные из сессии
    user_id = session.get('user_id')
    user_name = session.get('name')
    room = session.get('room_code')
    
    if not user_id or not room:
        # Если нет данных в сессии, отключаем SocketIO
        disconnect()
        return
        
    join_room(room)
    # Записываем текущую комнату пользователя
    user_room_map[user_id] = room 

    # Отправляем системное сообщение о присоединении (только в эту комнату)
    emit('new_message', {
        'sender_id': 'Система',
        'sender': 'Система', 
        'text': f'{user_name} присоединился к комнате.', 
        'timestamp': datetime.now(timezone.utc).strftime('%H:%M:%S')
    }, room=room)
    print(f'{user_name} joined room {room}')


@socketio.on('message_sent')
def handle_message(data):
    """Принимает сообщение, сохраняет его в БД и рассылает в комнату."""
    # Получаем данные из сессии
    user_id = session.get('user_id')
    sender_name = session.get('name')
    message_text = data.get('message')
    room_code = session.get('room_code')
    
    if not user_id or not room_code or not message_text:
        return

    # 1. Сохраняем в базу данных (С КОДОМ КОМНАТЫ)
    new_message = Message(
        sender_id=user_id,
        sender_name=sender_name, 
        text=message_text, 
        room_code=room_code
    )
    db.session.add(new_message)
    db.session.commit()

    # 2. Отправляем только в эту комнату
    emit('new_message', {
        'sender_id': user_id,
        'sender': sender_name,
        'text': message_text,
        'timestamp': new_message.timestamp.strftime('%H:%M:%S') 
    }, room=room_code) 


@socketio.on('typing_start')
def handle_typing_start():
    """Рассылает сообщение, что пользователь начал печатать."""
    user_name = session.get('name')
    room_code = session.get('room_code')
    
    if not room_code or not user_name:
        return
        
    emit('typing_update', {
        'username': user_name,
        'is_typing': True
    }, room=room_code, include_self=False) 


@socketio.on('typing_stop')
def handle_typing_stop():
    """Рассылает сообщение, что пользователь закончил печатать."""
    user_name = session.get('name')
    room_code = session.get('room_code')
    
    if not room_code or not user_name:
        return
        
    emit('typing_update', {
        'username': user_name,
        'is_typing': False
    }, room=room_code, include_self=False)

# --- 7. Запуск ---
if __name__ == '__main__':
    # В Railway таблицы должны быть созданы вручную или через миграции, 
    # чтобы избежать ошибок при запуске.
    
    # Запуск SocketIO
    socketio.run(app, debug=True, host='0.0.0.0', port=os.getenv("PORT", 5000))