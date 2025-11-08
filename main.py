import os
# --- КРИТИЧНОЕ ИСПРАВЛЕНИЕ: Eventlet Monkey-Patching ---
# Этот вызов должен быть выполнен ПЕРЕД ВСЕМИ ОСТАЛЬНЫМИ ИМПОРТАМИ.
import eventlet
eventlet.monkey_patch()
# --------------------------------------------------------

import secrets
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase # Импортируем DeclarativeBase для чистоты кода

from flask import Flask, render_template, redirect, url_for, request, session, flash
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

# --- 1. Глобальные, НЕПРИВЯЗАННЫЕ объекты (для фабрики) ---
# SQLAlchemy и SocketIO инициализируются без привязки к приложению.
# Это необходимо, чтобы избежать ошибки контекста при импорте.
db = SQLAlchemy() 
socketio = SocketIO(cors_allowed_origins="*") 

# Глобальные словари для отслеживания состояния 
user_room_map = {} 
typing_users_in_rooms = {} 


# --- 2. ФАБРИЧНАЯ ФУНКЦИЯ ДЛЯ СОЗДАНИЯ ПРИЛОЖЕНИЯ (ОСНОВА) ---

def create_app(test_config=None):
    """
    Фабричная функция для создания и настройки приложения Flask. 
    Все компоненты, включая модели, инициализируются внутри.
    """
    app = Flask(__name__)
    
    # --- Конфигурация Flask ---
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", secrets.token_hex(16))
    
    database_url = os.getenv("DATABASE_URL")
    if database_url and database_url.startswith("postgres://"):
        # Исправляем формат URL для совместимости с SQLAlchemy
        database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)

    app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///chat.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # --- Привязка объектов к приложению ---
    db.init_app(app)
    socketio.init_app(app, async_mode='eventlet')
    
    # --- 3. МОДЕЛИ ПЕРЕЕЗЖАЮТ ВНУТРЬ ФАБРИКИ (КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ) ---
    # Это гарантирует, что модели определяются ТОЛЬКО после того, как db.init_app(app) был вызван.
    
    # SQLAlchemy Model Base (необязательно, но полезно для современных версий)
    class Base(DeclarativeBase):
        pass
    
    # Убедимся, что db использует Base, если это необходимо.
    # В Flask-SQLAlchemy это обрабатывается автоматически, но для ясности:
    # db.Model - это уже правильная база.

    class User(db.Model):
        __tablename__ = 'users'
        id = db.Column(db.String(36), primary_key=True)
        google_id = db.Column(db.String(128), unique=True, nullable=True)
        name = db.Column(db.String(80), nullable=False)
        unique_name = db.Column(db.String(80), unique=True, nullable=True) 

        messages = db.relationship('Message', backref='author', lazy=True)

        def __repr__(self):
            return f'<User {self.name}>'

    class Message(db.Model):
        __tablename__ = 'messages'
        id = db.Column(db.Integer, primary_key=True)
        room_name = db.Column(db.String(128), nullable=False)
        user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
        content = db.Column(db.Text, nullable=False)
        timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

        def to_dict(self):
            # Внимание: здесь мы используем `db` и `User`, которые определены в фабрике. 
            # Поскольку мы находимся в одном потоке создания, это безопасно.
            return {
                'user_id': self.user_id,
                'user_name': self.author.unique_name or self.author.name if self.author else 'Неизвестный',
                'content': self.content,
                'timestamp': self.timestamp.strftime('%H:%M:%S'), 
            }
    
    # --- 4. Регистрация Обработчиков БД и Роутов ---

    @app.before_request
    def ensure_tables_exist():
        if not hasattr(app, 'tables_created') or not app.tables_created:
            with app.app_context():
                try:
                    # Создание таблиц
                    db.create_all() 
                    print("Таблицы базы данных созданы (или уже существовали).")
                    app.tables_created = True
                except Exception as e:
                    print(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось создать таблицы БД: {e}")
                    # В случае ошибки продолжим работу, чтобы не блокировать приложение
                    pass
    
    # --- Роуты Flask ---
    @app.route('/')
    def index():
        if 'user_id' in session:
            return render_template('room_selection.html', user_name=session.get('user_name'))
        return render_template('login.html')

    @app.route('/login', methods=['POST'])
    def login():
        username = request.form.get('username').strip()
        if not username or len(username) < 3 or len(username) > 30:
            flash('Имя пользователя должно быть от 3 до 30 символов.', 'error')
            return redirect(url_for('index'))
        
        unique_name = username.lower()
        # Модели доступны здесь, так как они определены внутри create_app
        user = User.query.filter_by(unique_name=unique_name).first()

        if not user:
            user_id = secrets.token_urlsafe(16)
            user = User(id=user_id, name=username, unique_name=unique_name) 
            try:
                db.session.add(user)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                flash('Произошла ошибка при создании пользователя.', 'error')
                return redirect(url_for('index'))
        
        session['user_id'] = user.id
        session['user_name'] = user.name
        session['unique_name'] = user.unique_name
        
        return redirect(url_for('index'))

    @app.route('/logout')
    def logout():
        session.pop('user_id', None)
        session.pop('user_name', None)
        session.pop('unique_name', None)
        flash('Вы вышли из системы.', 'success')
        return redirect(url_for('index'))

    @app.route('/join', methods=['POST'])
    def handle_join_request():
        if 'user_id' not in session:
            flash('Сначала войдите в систему.', 'error')
            return redirect(url_for('index'))

        room_name = request.form.get('room_name', '').strip()

        if not room_name or len(room_name) > 50:
            flash('Имя комнаты не может быть пустым или слишком длинным.', 'error')
            return redirect(url_for('index'))
        
        return redirect(url_for('chat_room', room_name=room_name))

    @app.route('/chat/<room_name>')
    def chat_room(room_name):
        if 'user_id' not in session:
            flash('Сначала войдите в систему.', 'error')
            return redirect(url_for('index'))

        return render_template(
            'chat.html', 
            room_name=room_name, 
            user_id=session['user_id']
        )
    
    # --- 5. Регистрация SocketIO обработчиков ---
    register_socketio_events(socketio, app, User, Message)
    
    return app


# --- 6. Отдельная функция для регистрации SocketIO событий ---
# Теперь она принимает классы моделей в качестве аргументов!
def register_socketio_events(socketio_instance, app_instance, User_Model, Message_Model):
    """
    Регистрирует все обработчики SocketIO.
    """
    
    @socketio_instance.on('join')
    def on_join(data):
        user_id = session.get("user_id")
        user_name = session.get("user_name")
        room_name = data.get('room')

        if not all([user_id, user_name, room_name]):
            emit('error_message', {'msg': 'Не удалось присоединиться к комнате.'})
            return

        # ... (логика присоединения/покидания комнаты) ...
        if user_id in user_room_map and user_room_map[user_id] != room_name:
            old_room = user_room_map[user_id]
            leave_room(old_room)
            emit('status_message', {'msg': f'{user_name} покинул комнату.'}, room=old_room)
            
        join_room(room_name)
        user_room_map[user_id] = room_name 
        
        emit('status_message', 
             {'msg': f'{user_name} присоединился к комнате.'}, 
             room=room_name, include_self=False) 

        # Загрузка истории сообщений (используем Message_Model)
        with app_instance.app_context():
            # Загружаем последние 50 сообщений
            messages = Message_Model.query.filter_by(room_name=room_name).order_by(Message_Model.timestamp.desc()).limit(50).all()
            messages.reverse() 
            history = [msg.to_dict() for msg in messages]
            emit('message_history', {'history': history, 'user_name': user_name})

    @socketio_instance.on('disconnect')
    def handle_disconnect():
        user_id = session.get("user_id")
        user_name = session.get("user_name")
        
        if user_id in user_room_map:
            room_name = user_room_map.pop(user_id)
            
            if room_name in typing_users_in_rooms and user_id in typing_users_in_rooms[room_name]:
                typing_users_in_rooms[room_name].pop(user_id, None)
                emit('typing_status', 
                     {'user_id': user_id, 'user_name': user_name, 'is_typing': False}, 
                     room=room_name, include_self=False)
            
            emit('status_message', {'msg': f'{user_name} покинул комнату {room_name}.'}, room=room_name)

    @socketio_instance.on('send_message')
    def handle_message(data):
        user_id = session.get("user_id")
        room_name = user_room_map.get(user_id)
        content = data.get('content', '').strip()
        user_name = session.get("user_name")

        if not all([user_id, room_name, content, user_name]) or len(content) > 500:
            return 

        with app_instance.app_context():
            # Используем User_Model
            user = User_Model.query.get(user_id)
            if not user:
                return

            # Используем Message_Model
            new_message = Message_Model(room_name=room_name, user_id=user_id, content=content)
            try:
                db.session.add(new_message)
                db.session.commit()
                message_data = new_message.to_dict()
                socketio_instance.emit('new_message', message_data, room=room_name)
            except Exception as e:
                db.session.rollback()
                print(f"Ошибка базы данных при сохранении сообщения: {e}")
                emit('error_message', {'msg': 'Ошибка сервера при сохранении сообщения.'})
    
    # ... (логика индикаторов печати, без запросов к БД)
    @socketio_instance.on('start_typing')
    def handle_start_typing():
        user_id = session.get("user_id")
        user_name = session.get("user_name")
        room_name = user_room_map.get(user_id)
        
        if not all([user_id, room_name]): return
        if room_name not in typing_users_in_rooms: typing_users_in_rooms[room_name] = {}
            
        typing_users_in_rooms[room_name][user_id] = user_name
        
        emit('typing_status', 
             {'user_id': user_id, 'user_name': user_name, 'is_typing': True}, 
             room=room_name, include_self=False)

    @socketio_instance.on('stop_typing')
    def handle_stop_typing():
        user_id = session.get("user_id")
        room_name = user_room_map.get(user_id)
        
        if not all([user_id, room_name]): return
            
        if room_name in typing_users_in_rooms and user_id in typing_users_in_rooms[room_name]:
            typing_users_in_rooms[room_name].pop(user_id, None)
        
        emit('typing_status', 
             {'user_id': user_id, 'user_name': session['user_name'], 'is_typing': False}, 
             room=room_name, include_self=False)


# --- 7. Запуск приложения ---

# Gunicorn импортирует ЭТУ переменную 'app'
app = create_app()

if __name__ == '__main__':
    # Для локального запуска используем socketio.run
    print("Локальный запуск Eventlet/SocketIO...")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)