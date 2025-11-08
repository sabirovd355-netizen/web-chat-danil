import os
import json
import secrets
from datetime import datetime, timezone

# --- КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: eventlet.monkey_patch() ДОЛЖЕН БЫТЬ ПЕРВЫМ ИМПОРТОМ ---
import eventlet
eventlet.monkey_patch()
# ----------------------------------------------------------------------------------

from flask import Flask, render_template, redirect, url_for, request, session, abort, flash
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect, rooms
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
# Используем eventlet для SocketIO, так как это указано в Procfile
# Убедитесь, что worker_class в Procfile совпадает с используемым SocketIO движком
socketio = SocketIO(app) 

# Конфигурация Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
# Важно: В рабочей среде GOOGLE_REDIRECT_URI должен быть установлен через Railway Variables
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:5000/google/callback") 
if not GOOGLE_CLIENT_ID:
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

client = WebApplicationClient(GOOGLE_CLIENT_ID)

# Глобальная карта: Хранит, в какой комнате находится пользователь (по ID)
user_room_map = {} 

# --- 2. Модели и База данных ---

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    profile_pic = db.Column(db.String(255))

    def __repr__(self):
        return f'<User {self.name}>'

class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    room_name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('messages', lazy=True))
    content = db.Column(db.Text, nullable=False)
    # Используем UTC для единообразия
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc)) 

    def to_dict(self):
        return {
            'id': self.id,
            'user_name': self.user.name,
            'user_pic': self.user.profile_pic,
            'content': self.content,
            'timestamp': self.timestamp.astimezone(timezone.utc).strftime('%H:%M'),
        }

# --- АВТОМАТИЧЕСКОЕ СОЗДАНИЕ ТАБЛИЦ ---
with app.app_context():
    try:
        db.create_all()
        print("Проверка/создание таблиц базы данных завершена.")
    except Exception as e:
        print(f"Ошибка при создании таблиц: {e}")
        pass


# --- 3. Функции OAuth и Аутентификация ---

def get_google_provider_cfg():
    """Получает конфигурацию Google OpenID."""
    response = requests.get(GOOGLE_DISCOVERY_URL)
    response.raise_for_status()
    return response.json()

def get_or_create_user(google_id, name, picture):
    """Ищет пользователя по google_id или создает нового."""
    user = User.query.filter_by(google_id=google_id).first()
    if not user:
        user = User(google_id=google_id, name=name, profile_pic=picture)
        db.session.add(user)
        db.session.commit()
    return user


# --- 4. Маршруты Flask (ОБНОВЛЕНЫ) ---

@app.route("/")
def index():
    """Главная страница. Если пользователь вошел, перенаправляет в меню выбора комнаты."""
    if "user_id" in session:
        # Перенаправляем на новое меню выбора комнаты
        return redirect(url_for("room_choice_menu")) 
    
    # Для отображения кнопки входа
    google_config = get_google_provider_cfg()
    authorization_endpoint = google_config["authorization_endpoint"]
    
    return render_template("index.html", 
                           authorization_endpoint=authorization_endpoint,
                           client_id=GOOGLE_CLIENT_ID,
                           redirect_uri=GOOGLE_REDIRECT_URI)

@app.route("/google/login")
def login():
    """Начинает процесс Google OAuth."""
    google_config = get_google_provider_cfg()
    authorization_endpoint = google_config["authorization_endpoint"]

    # Использование библиотеки oauthlib для построения URL-адреса
    request_uri = client.prepare_request_uri(
        authorization_endpoint,
        redirect_uri=GOOGLE_REDIRECT_URI,
        scope=["openid", "email", "profile"],
        state=secrets.token_urlsafe(16)
    )
    return redirect(request_uri)

@app.route("/google/callback")
def callback():
    """Обрабатывает обратный вызов от Google."""
    code = request.args.get("code")
    
    if not code:
        flash("Ошибка: Не получен код авторизации от Google. (Проверьте GOOGLE_REDIRECT_URI в Google Console и на хостинге)", 'error')
        return redirect(url_for("index"))

    # Находим URL-адрес для обмена кодом на токен
    google_config = get_google_provider_cfg()
    token_endpoint = google_config["token_endpoint"]

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
        auth=(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET),
    )
    token_response.raise_for_status()

    # Парсим токен
    client.parse_request_body_response(json.dumps(token_response.json()))
    
    # Получаем информацию о пользователе
    userinfo_endpoint = google_config["userinfo_endpoint"]
    uri, headers, body = client.add_token(userinfo_endpoint)
    userinfo_response = requests.get(uri, headers=headers, data=body)
    userinfo_response.raise_for_status()
    
    user_info = userinfo_response.json()
    
    # Получаем данные, необходимые для создания/аутентификации пользователя
    google_id = user_info["sub"]
    name = user_info.get("given_name") or user_info.get("name", "Гость")
    picture = user_info["picture"]

    # Сохраняем пользователя в нашей БД и устанавливаем сессию
    user = get_or_create_user(google_id, name, picture)
    session["user_id"] = user.id
    session["user_name"] = user.name
    session["user_pic"] = user.profile_pic

    # После успешного входа перенаправляем на выбор комнаты
    return redirect(url_for("room_choice_menu")) 

@app.route("/logout")
def logout():
    """Выход из системы."""
    # Также очищаем информацию о текущей комнате
    if "current_room" in session:
        session.pop("current_room")
        
    session.clear()
    flash("Вы успешно вышли из системы.", 'info')
    return redirect(url_for("index"))

@app.route("/menu")
def room_choice_menu():
    """Меню выбора: Создать комнату или Присоединиться (Рендерит room_choice.html)."""
    if "user_id" not in session:
        flash("Пожалуйста, войдите в систему, чтобы выбрать комнату.", 'warning')
        return redirect(url_for("index")) 
        
    # Удаляем информацию о старой комнате при переходе в меню
    if "current_room" in session:
        session.pop("current_room")
        
    return render_template("room_choice.html")


@app.route("/chat/<room_id>")
def chat(room_id):
    """Страница чата для конкретной комнаты."""
    if "user_id" not in session:
        flash("Пожалуйста, войдите в систему, чтобы использовать чат.", 'warning')
        return redirect(url_for("index"))
    
    room_name = room_id
    
    # !!! Сохраняем имя комнаты в сессии для SocketIO !!!
    session["current_room"] = room_name
    
    # Загружаем последние 50 сообщений для отображения истории
    messages = Message.query.filter_by(room_name=room_name) \
                            .order_by(Message.timestamp.desc()) \
                            .limit(50) \
                            .all()
    
    # Обратный порядок для правильного отображения (старые сверху)
    messages.reverse() 

    # ВАЖНО: Вам нужно будет создать файл chat.html для работы чата
    return render_template("chat.html", 
                           user_name=session["user_name"],
                           user_pic=session["user_pic"],
                           room_name=room_name,
                           messages=[m.to_dict() for m in messages])

# --- 5. Обработчики SocketIO (ОБНОВЛЕНЫ) ---

@socketio.on('connect')
def handle_connect():
    """
    Обрабатывает подключение нового пользователя.
    Использует session["current_room"], установленную маршрутом /chat/<room_id>.
    """
    user_id = session.get("user_id")
    room_name = session.get("current_room") 

    if not user_id or not room_name:
        # Отключаем, если нет аутентификации или комнаты
        print("Неаутентифицированный пользователь или отсутствует комната отключен.")
        disconnect()
        return

    # Присоединяем пользователя к нужной комнате
    join_room(room_name)
    user_room_map[user_id] = room_name

    # Сообщаем всем в комнате, что кто-то присоединился
    emit('status_message', 
         {'msg': f'{session["user_name"]} присоединился к комнате {room_name}.'}, 
         room=room_name)
    print(f'Пользователь {session["user_name"]} подключен к комнате {room_name}.')


@socketio.on('disconnect')
def handle_disconnect():
    """Обрабатывает отключение пользователя."""
    user_id = session.get("user_id")
    if user_id in user_room_map:
        room_name = user_room_map.pop(user_id)
        
        # Сообщаем всем, что пользователь отключился
        emit('status_message', 
             {'msg': f'{session["user_name"]} покинул комнату {room_name}.'}, 
             room=room_name)
        
        print(f'Пользователь {session["user_name"]} отключен от комнаты {room_name}.')

@socketio.on('send_message')
def handle_message(data):
    """Обрабатывает отправку нового сообщения."""
    user_id = session.get("user_id")
    room_name = user_room_map.get(user_id)
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
        user_id=user_id,
        content=content
    )
    db.session.add(new_message)
    db.session.commit()
    
    # 2. Отправляем сообщение всем в комнате
    message_data = new_message.to_dict()

    emit('new_message', message_data, room=room_name)

# --- 6. Запуск приложения ---

if __name__ == "__main__":
    # Локально запускаем через SocketIO.run, а на Railway - через gunicorn
    socketio.run(app, debug=True)