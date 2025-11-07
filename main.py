import os
import json
from datetime import datetime

from flask import Flask, render_template, redirect, url_for, request, session, abort
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy

from oauthlib.oauth2 import WebApplicationClient
import requests

# --- 1. Конфигурация и Инициализация ---
app = Flask(__name__)
# !!! СЕКРЕТНЫЙ КЛЮЧ !!!
app.config['SECRET_KEY'] = 'aB3c9D1e8F4g0H7i2J5k6LmM0nNpP1qQ2rR3sS4tT5uU6vV7wW8xX9yY0zZ' 

# --- КОНФИГУРАЦИЯ GOOGLE OAUTH (КЛЮЧИ СОХРАНЕНЫ) ---
app.config['GOOGLE_CLIENT_ID'] = os.getenv("GOOGLE_CLIENT_ID")
app.config['GOOGLE_CLIENT_SECRET'] = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:5000/google/callback")

# !!! РАЗРЕШАЕМ HTTP для локальной разработки (ИСПРАВЛЕНИЕ InsecureTransportError) !!!
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

GOOGLE_DISCOVERY_URL = (
    "https://accounts.google.com/.well-known/openid-configuration"
)
client = WebApplicationClient(app.config['GOOGLE_CLIENT_ID'])


# --- КОНФИГУРАЦИЯ БАЗЫ ДАННЫХ ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL") or 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

socketio = SocketIO(app)

# !!! ГЛОБАЛЬНАЯ КАРТА: Хранит, в какой комнате находится пользователь (по ID) !!!
user_room_map = {} 


# --- 2. Модели и База данных ---

class User(db.Model):
    """Модель пользователя."""
    id = db.Column(db.String(128), primary_key=True) # ID от Google
    name = db.Column(db.String(80), nullable=False)

class Message(db.Model):
    """Модель сообщения для истории."""
    id = db.Column(db.Integer, primary_key=True)
    sender_name = db.Column(db.String(80), nullable=False)
    text = db.Column(db.String(500), nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    # !!! НОВЫЙ СТОЛБЕЦ: Код комнаты !!!
    room_code = db.Column(db.String(50), nullable=False) 

# Создаем базу данных и таблицы, если их нет
with app.app_context():
    db.create_all()


# --- 3. Аутентификация Flask-Login ---
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, user_id)

def get_google_provider_cfg():
    """Получает конфигурацию Google API."""
    return requests.get(GOOGLE_DISCOVERY_URL).json()

# --- 4. Маршруты Аутентификации GOOGLE OAUTH ---
@app.route('/login')
def login():
    """Инициирует процесс входа через Google."""
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
        auth=(app.config['GOOGLE_CLIENT_ID'], app.config['GOOGLE_CLIENT_SECRET'])
    ).json()

    client.parse_request_body_response(json.dumps(token_response))

    # Получаем информацию о пользователе
    userinfo_endpoint = google_provider_cfg["userinfo_endpoint"]
    uri, headers, body = client.add_token(userinfo_endpoint)
    userinfo_response = requests.get(uri, headers=headers, data=body).json()

    # Проверяем, есть ли у пользователя email
    if not userinfo_response.get("email_verified"):
        return abort(500, description="Пользователь не подтвердил email в Google.")

    unique_id = userinfo_response["sub"]
    user_name = userinfo_response["name"]
    
    # Создаем или загружаем пользователя
    user = db.session.get(User, unique_id)
    if not user:
        user = User(id=unique_id, name=user_name)
        db.session.add(user)
        db.session.commit()

    # Входим
    login_user(user)
    # Перенаправляем на выбор комнаты
    return redirect(url_for("choose_room"))

@app.route('/logout')
@login_required
def logout():
    """Выход пользователя."""
    logout_user()
    return redirect(url_for('login'))

# --- 5. МАРШРУТЫ ДЛЯ КОМНАТ ---

@app.route('/')
@login_required
def choose_room():
    """Страница, где пользователь выбирает или создает комнату (room_choice.html)."""
    return render_template('room_choice.html', username=current_user.name)

@app.route('/chat/<room_code>')
@login_required
def index(room_code):
    """Главная страница мессенджера (index.html) с кодом комнаты."""
    # 1. Загружаем историю только для этой комнаты
    history = Message.query.filter_by(room_code=room_code) \
                           .order_by(Message.timestamp.desc()).limit(50).all()
    history.reverse()
    
    # 2. Передаем код комнаты в шаблон
    return render_template('index.html', 
                           username=current_user.name, 
                           room_code=room_code,
                           history=history)

# --- 6. ОБРАБОТЧИКИ WebSockets ДЛЯ КОМНАТ ---

@socketio.on('join')
@login_required
def on_join(data):
    """Пользователь присоединяется к комнате SocketIO."""
    room = data.get('room')
    if not room:
        return
        
    join_room(room)
    # Записываем текущую комнату пользователя
    user_room_map[current_user.id] = room 

    # Отправляем системное сообщение о присоединении (только в эту комнату)
    emit('new_message', {
        'sender': 'Система', 
        'text': f'{current_user.name} присоединился к комнате.', 
        'timestamp': datetime.now().strftime('%H:%M:%S')
    }, room=room)


@socketio.on('message_sent')
@login_required
def handle_message(data):
    """Принимает сообщение, сохраняет его в БД и рассылает в комнату."""
    sender_name = current_user.name 
    message_text = data.get('message')
    
    # Получаем комнату из карты
    room_code = user_room_map.get(current_user.id)
    if not room_code or not message_text:
        return

    # 1. Сохраняем в базу данных (С КОДОМ КОМНАТЫ)
    new_message = Message(sender_name=sender_name, text=message_text, room_code=room_code)
    db.session.add(new_message)
    db.session.commit()

    # 2. Отправляем только в эту комнату
    emit('new_message', {
        'sender': sender_name,
        'text': message_text,
        'timestamp': new_message.timestamp.strftime('%H:%M:%S') 
    }, room=room_code) 


@socketio.on('typing_start')
@login_required
def handle_typing_start(): # <<< ИСПРАВЛЕНО: УДАЛЕН АРГУМЕНТ 'data'
    """Рассылает сообщение, что пользователь начал печатать."""
    room_code = user_room_map.get(current_user.id)
    if not room_code:
        return
        
    emit('typing_update', {
        'username': current_user.name,
        'is_typing': True
    }, room=room_code, include_self=False) 


@socketio.on('typing_stop')
@login_required
def handle_typing_stop(): # <<< ИСПРАВЛЕНО: УДАЛЕН АРГУМЕНТ 'data'
    """Рассылает сообщение, что пользователь закончил печатать."""
    room_code = user_room_map.get(current_user.id)
    if not room_code:
        return
        
    emit('typing_update', {
        'username': current_user.name,
        'is_typing': False
    }, room=room_code, include_self=False)

# --- 7. Запуск ---
if __name__ == '__main__':
    socketio.run(app, debug=True)