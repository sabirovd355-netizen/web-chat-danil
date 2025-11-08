import os
import secrets
from datetime import datetime, timezone
from sqlalchemy.orm import DeclarativeBase 
import eventlet
# eventlet monkey-patching должно идти ДО ЛЮБЫХ других импортов,
# использующих стандартные блокирующие функции (например, socket).
eventlet.monkey_patch() 

from flask import Flask, render_template, redirect, url_for, request, session, flash
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

# --- Глобальные, НЕПРИВЯЗАННЫЕ объекты (для фабрики) ---
# SQLAlchemy и SocketIO инициализируются без привязки к приложению.
db = SQLAlchemy() 
socketio = SocketIO(cors_allowed_origins="*") 

# --- ФАБРИЧНАЯ ФУНКЦИЯ ДЛЯ СОЗДАНИЯ ПРИЛОЖЕНИЯ (ОСНОВА) ---

def create_app():
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
    
    # --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ЧАТА (ВНУТРИ ФАБРИКИ) ---
    # Переносим сюда, чтобы они были доступны только после инициализации
    user_room_map = {} 
    typing_users_in_rooms = {} 
    
    # --- МОДЕЛИ (ВНУТРИ ФАБРИКИ) ---
    
    class Base(DeclarativeBase):
        pass
    
    class User(db.Model):
        __tablename__ = 'users'
        id = db.Column(db.String(36), primary_key=True)
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
            # Проверяем наличие автора, чтобы избежать ошибки при удалении пользователя
            author_name = self.author.unique_name if self.author and self.author.unique_name else 'Неизвестный'
            return {
                'user_id': self.user_id,
                'user_name': author_name,
                'content': self.content,
                'timestamp': self.timestamp.strftime('%H:%M:%S'), 
            }
    
    # --- Регистрация Обработчиков БД и Роутов ---

    @app.before_request
    def ensure_tables_exist():
        if not hasattr(app, 'tables_created') or not app.tables_created:
            with app.app_context():
                try:
                    db.create_all() 
                    print("Таблицы базы данных созданы (или уже существовали).")
                    app.tables_created = True
                except Exception as e:
                    print(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось создать таблицы БД: {e}")
                    pass
    
    # --- Роуты Flask ---
    @app.route('/')
    def index():
        if 'user_id' in session:
            # Перенаправляем на новое имя шаблона
            return render_template('room_selection.html', user_name=session.get('user_name'))
        # Используем обновленный шаблон login.html
        return render_template('login.html')

    @app.route('/login', methods=['POST'])
    def login():
        username = request.form.get('username').strip()
        if not username or len(username) < 3 or len(username) > 30:
            flash('Имя пользователя должно быть от 3 до 30 символов.', 'error')
            return redirect(url_for('index'))
        
        unique_name = username.lower()
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
        
        # Перенаправляем на выбор комнаты
        return redirect(url_for('index'))

    @app.route('/logout')
    def logout():
        # НЕ ВЫЗЫВАЙТЕ socketio.disconnect() в HTTP-роуте, это вызовет ошибку.
        # Просто очищаем сессию. Сокет сам обработает отключение.
        if 'user_id' in session and session['user_id'] in user_room_map:
            # Пытаемся удалить пользователя из карты, чтобы избежать "мертвых" записей
            user_room_map.pop(session['user_id'], None)

        session.pop('user_id', None)
        session.pop('user_name', None)
        session.pop('unique_name', None)
        flash('Вы вышли из системы.', 'success')
        return redirect(url_for('index'))

    # Роут для выбора комнаты (если пользователь уже вошел)
    @app.route('/room_selection', methods=['GET', 'POST'])
    def room_selection():
        if 'user_id' not in session:
            flash('Сначала войдите в систему.', 'error')
            return redirect(url_for('index'))
        
        if request.method == 'POST':
            room_name = request.form.get('room_name', '').strip()

            if not room_name or len(room_name) > 50:
                flash('Имя комнаты не может быть пустым или слишком длинным.', 'error')
                return redirect(url_for('room_selection'))
            
            # Перенаправляем в комнату
            return redirect(url_for('chat_room', room_name=room_name))

        # При GET-запросе показываем шаблон выбора
        return render_template(
            'room_selection.html', 
            user_name=session.get('user_name'),
            popular_rooms=['General', 'Development', 'Random']
        )
    
    # Роут для отображения чат-комнаты
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
    
    # --- SocketIO обработчики (ВНУТРИ ФАБРИКИ) ---

    @socketio.on('join')
    def on_join(data):
        user_id = session.get("user_id")
        user_name = session.get("user_name")
        room_name = data.get('room')

        if not all([user_id, user_name, room_name]):
            emit('error_message', {'msg': 'Не удалось присоединиться к комнате.'})
            return

        # 1. Сначала выходим из старой комнаты, если она была
        if user_id in user_room_map and user_room_map[user_id] != room_name:
            old_room = user_room_map[user_id]
            leave_room(old_room)
            # Отправляем сообщение об уходе в старую комнату
            emit('status_message', {'msg': f'{user_name} покинул комнату.'}, room=old_room)
            
        # 2. Присоединяемся к новой комнате
        join_room(room_name)
        user_room_map[user_id] = room_name 
        
        # 3. Отправляем сообщение о присоединении всем, кроме себя
        emit('status_message', 
             {'msg': f'{user_name} присоединился к комнате.'}, 
             room=room_name, include_self=False) 

        # 4. Загрузка истории сообщений (Теперь модели ДОСТУПНЫ)
        with app.app_context():
            # Загружаем последние 50 сообщений
            messages = Message.query.filter_by(room_name=room_name).order_by(Message.timestamp.desc()).limit(50).all()
            messages.reverse() 
            history = [msg.to_dict() for msg in messages]
            emit('message_history', {'history': history, 'user_name': user_name})

    @socketio.on('disconnect')
    def handle_disconnect():
        user_id = session.get("user_id")
        user_name = session.get("user_name")
        
        # Обработка состояния отключения и удаления из карты
        if user_id in user_room_map:
            room_name = user_room_map.pop(user_id)
            
            # Удаляем пользователя из индикаторов печати
            if room_name in typing_users_in_rooms and user_id in typing_users_in_rooms[room_name]:
                typing_users_in_rooms[room_name].pop(user_id, None)
                emit('typing_status', 
                     {'user_id': user_id, 'user_name': user_name, 'is_typing': False}, 
                     room=room_name, include_self=False)
            
            # Сообщаем всем, что пользователь покинул комнату
            emit('status_message', {'msg': f'{user_name} покинул комнату {room_name}.'}, room=room_name)

    @socketio.on('send_message')
    def handle_message(data):
        user_id = session.get("user_id")
        room_name = user_room_map.get(user_id)
        content = data.get('content', '').strip()
        user_name = session.get("user_name")

        if not all([user_id, room_name, content, user_name]) or len(content) > 500:
            return 

        with app.app_context():
            # Теперь User и Message определены и доступны
            user = User.query.get(user_id)
            if not user:
                return

            new_message = Message(room_name=room_name, user_id=user_id, content=content)
            try:
                db.session.add(new_message)
                db.session.commit()
                message_data = new_message.to_dict()
                socketio.emit('new_message', message_data, room=room_name)
            except Exception as e:
                db.session.rollback()
                print(f"Ошибка базы данных при сохранении сообщения: {e}")
                emit('error_message', {'msg': 'Ошибка сервера при сохранении сообщения.'})
    
    # ... (логика индикаторов печати)
    @socketio.on('start_typing')
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

    @socketio.on('stop_typing')
    def handle_stop_typing():
        user_id = session.get("user_id")
        room_name = user_room_map.get(user_id)
        
        if not all([user_id, room_name]): return
            
        if room_name in typing_users_in_rooms and user_id in typing_users_in_rooms[room_name]:
            typing_users_in_rooms[room_name].pop(user_id, None)
        
        emit('typing_status', 
             {'user_id': user_id, 'user_name': session['user_name'], 'is_typing': False}, 
             room=room_name, include_self=False)


    return app


# --- Запуск приложения ---

# Gunicorn импортирует ЭТУ переменную 'app'
app = create_app()

if __name__ == '__main__':
    # Для локального запуска используем socketio.run
    print("Локальный запуск Eventlet/SocketIO...")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)