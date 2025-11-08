import os
import eventlet
# --- КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Eventlet Monkey-Patching ---
# Этот вызов должен быть выполнен ПЕРЕД ВСЕМИ ОСТАЛЬНЫМИ ИМПОРТАМИ,
# чтобы обеспечить асинхронную работу Flask-SocketIO с Eventlet (как указано в Procfile).
eventlet.monkey_patch() 

import json
import secrets
from datetime import datetime, timezone

from flask import Flask, render_template, redirect, url_for, request, session, flash
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect, rooms
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash # Добавляем для использования

# --- 1. Конфигурация и Инициализация ---
app = Flask(__name__)

# ИСПРАВЛЕНИЕ ДЛЯ RAILWAY (HTTPS):
# Это необходимо, чтобы Flask видел, что запрос пришел по HTTPS.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)

# --- КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: КОНФИГУРАЦИЯ БАЗЫ ДАННЫХ ---
# В продакшене (Railway) используется переменная окружения DATABASE_URL.
# В разработке используется локальная SQLite.
if os.environ.get('DATABASE_URL'):
    # PostgreSQL требует, чтобы URL был в формате, который понимает SQLAlchemy.
    # Заменяем 'postgres://' на 'postgresql://' для совместимости с новыми версиями SQLAlchemy.
    db_url = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    print("INFO: Используется PostgreSQL.")
else:
    # Используется локальная база данных SQLite для разработки
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site.db'
    print("INFO: Используется локальная база данных SQLite.")

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(16))
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*") # cors_allowed_origins="*" для работы с SocketIO в Production

# --- 2. Модели базы данных ---
class User(db.Model):
    """Модель пользователя для хранения имени и ID"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    # Добавьте эти поля, если они у вас есть в коде:
    # email = db.Column(db.String(120), unique=True, nullable=True) 
    # password_hash = db.Column(db.String(128), nullable=True) 
    
    def __repr__(self):
        return f'<User {self.username}>'

class Message(db.Model):
    """Модель сообщения для хранения истории чата"""
    id = db.Column(db.Integer, primary_key=True)
    room_name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.String(500), nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.now(timezone.utc))

    user = db.relationship('User', backref=db.backref('messages', lazy=True))

    def __repr__(self):
        return f'<Message {self.content[:20]} in {self.room_name}>'

# --- 3. Глобальные словари для отслеживания состояния ---
user_room_map = {}
typing_users_in_rooms = {}
ROOM_LIST = ["PythonDev", "General", "Random", "Frontend", "Backend"]

# --- 4. Маршруты (Routes) ---

@app.route('/')
@app.route('/index')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('index.html', title='Главная')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Ваша логика входа
    return render_template('login.html', title='Вход')

@app.route('/room_selection')
def room_selection():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('room_selection.html', rooms=ROOM_LIST, title='Выбор комнаты')

@app.route('/chat/<room_name>')
def chat(room_name):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if room_name not in ROOM_LIST:
        flash('Такой комнаты не существует.', 'error')
        return redirect(url_for('room_selection'))
    
    # Загрузка истории сообщений (исправлено)
    try:
        messages = Message.query.filter_by(room_name=room_name).order_by(Message.timestamp.asc()).all()
    except Exception as e:
        # Это может случиться, если база данных еще не инициализирована или есть проблемы с миграцией.
        print(f"Ошибка при загрузке сообщений: {e}")
        messages = []

    return render_template('chat.html', room_name=room_name, messages=messages)

# --- 5. Обработчики SocketIO ---
@socketio.on('join')
def on_join(data):
    # Ваш код обработки присоединения
    username = session.get('username')
    room = data.get('room')
    if not username or not room:
        return 
    
    join_room(room)
    user_room_map[request.sid] = room
    
    emit('status', {'msg': username + ' присоединился к комнате.'}, room=room)

@socketio.on('text')
def on_text(data):
    # Ваш код обработки текстовых сообщений
    room = user_room_map.get(request.sid)
    username = session.get('username')
    content = data.get('msg')
    
    if not room or not username or not content:
        return

    # Сохранение сообщения в БД (пример)
    try:
        user = User.query.filter_by(username=username).first()
        if user:
            new_message = Message(room_name=room, user_id=user.id, content=content)
            db.session.add(new_message)
            db.session.commit()
    except Exception as e:
        print(f"Ошибка сохранения сообщения в БД: {e}")
        db.session.rollback()


    emit('message', {'msg': content, 'username': username, 'time': datetime.now(timezone.utc).strftime('%H:%M')}, room=room)

@socketio.on('disconnect')
def test_disconnect():
    # Ваш код обработки отключения
    room = user_room_map.pop(request.sid, None)
    username = session.get('username')
    if room and username:
        leave_room(room)
        emit('status', {'msg': username + ' покинул комнату.'}, room=room)

# --- 6. Запуск приложения ---
if __name__ == '__main__':
    with app.app_context():
        # Если вы используете SQLite локально и запускаете впервые, раскомментируйте это:
        # db.create_all()
        pass
    
    # В локальной разработке используем eventlet для запуска
    eventlet.wsgi.server(eventlet.listen(('', 5000)), app)