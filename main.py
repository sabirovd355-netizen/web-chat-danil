import os
from datetime import datetime
# Удаляем import eventlet и eventlet.monkey_patch() - они вызывают конфликты
# Gunicorn с воркером eventlet сам управляет патчингом.

from flask import Flask, render_template, request, session, redirect, url_for, flash
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash 
from werkzeug.middleware.proxy_fix import ProxyFix # Для работы сессий на Railway

# ----------------------------------------------------
# 1. Глобальная инициализация расширений
# ----------------------------------------------------
db = SQLAlchemy()
# Устанавливаем cors_allowed_origins="*" для работы с SocketIO в Production
socketio = SocketIO() 
ROOM_LIST = ["PythonDev", "General", "Random", "Frontend", "Backend"]

# ----------------------------------------------------
# 2. Модели базы данных
# ----------------------------------------------------

class User(db.Model):
    """Модель пользователя для хранения имени и ID"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)

    def __repr__(self):
        return f'<User {self.username}>'

class Message(db.Model):
    """Модель сообщения для хранения истории чата"""
    id = db.Column(db.Integer, primary_key=True)
    room_name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    user_name = db.Column(db.String(80), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)

    def to_dict(self):
        """Преобразование модели в словарь для отправки через SocketIO"""
        return {
            'room_name': self.room_name,
            'user_id': str(self.user_id), 
            'user_name': self.user_name,
            'content': self.content,
            'timestamp': self.timestamp.strftime('%H:%M:%S'),
        }

# ----------------------------------------------------
# 3. Фабрика приложений Flask
# ----------------------------------------------------

def create_app():
    app = Flask(__name__)

    # -------------------
    # ИСПРАВЛЕНИЕ ПРОКСИ (ДЛЯ RAILWAY/GUNICORN)
    # Это позволяет Flask видеть, что запрос пришел по HTTPS и корректно
    # обрабатывать заголовки сессий.
    # -------------------
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1, x_port=1)


    # --- Конфигурация базы данных и переменных окружения ---
    database_url = os.environ.get('SQLALCHEMY_DATABASE_URI') or \
                   os.environ.get('DATABASE_URL')

    if database_url is None:
        database_url = 'sqlite:///database.db'
        print("INFO: Используется локальная база данных SQLite.")
    else:
        # Исправление для Heroku/Railway Postgres
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        print(f"INFO: Используется Production DB URI: {database_url}")
        
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False 
    # ВАЖНО: Установите надежный SECRET_KEY в переменных окружения Railway
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default_secret_key_if_missing') 
    
    # Инициализация расширений Flask
    db.init_app(app) 
    socketio.init_app(app, cors_allowed_origins="*", logger=True, engineio_logger=True)
    
    # ----------------------------------------------------
    # 4. Маршруты (Routes)
    # ----------------------------------------------------

    @app.route('/', methods=['GET', 'POST'])
    def login():
        """Страница входа по имени пользователя."""
        if 'user_id' in session:
            return redirect(url_for('room_selection'))

        if request.method == 'POST':
            username = request.form.get('username')
            if not username or len(username.strip()) < 3 or len(username.strip()) > 30:
                flash('Имя пользователя должно быть от 3 до 30 символов.', 'error')
                return redirect(url_for('login'))

            username = username.strip()
            user = User.query.filter_by(username=username).first()

            if user is None:
                user = User(username=username)
                db.session.add(user)
                db.session.commit()
                flash(f'Добро пожаловать, {username}! Ваш аккаунт создан.', 'success')
            else:
                flash(f'С возвращением, {username}!', 'success')

            session['user_id'] = user.id
            session['username'] = user.username
            
            return redirect(url_for('room_selection'))

        return render_template('login.html')


    @app.route('/room_selection', methods=['GET', 'POST'])
    def room_selection():
        """Страница выбора комнаты."""
        if 'user_id' not in session:
            flash('Сначала войдите в систему.', 'error')
            return redirect(url_for('login'))

        if request.method == 'POST':
            room_name = request.form.get('room_name')
            if room_name and 3 <= len(room_name.strip()) <= 50:
                room_name = room_name.strip()
                return redirect(url_for('chat_room', room_name=room_name))
            else:
                flash('Имя комнаты должно быть от 3 до 50 символов.', 'error')

        # Используем room_selection.html, как в вашей структуре файлов
        return render_template('room_selection.html', 
                               user_name=session['username'], 
                               popular_rooms=ROOM_LIST)


    @app.route('/chat/<room_name>')
    def chat_room(room_name):
        """Страница чат-комнаты."""
        if 'user_id' not in session:
            flash('Сначала войдите в систему.', 'error')
            return redirect(url_for('login'))
        
        if not room_name or 3 > len(room_name) > 50:
            flash('Недопустимое имя комнаты.', 'error')
            return redirect(url_for('room_selection'))

        return render_template('chat.html', 
                               room_name=room_name,
                               user_id=session['user_id'])


    @app.route('/logout')
    def logout():
        """Выход из системы."""
        session.pop('user_id', None)
        session.pop('username', None)
        flash('Вы успешно вышли из системы.', 'info')
        return redirect(url_for('login'))
    
    # ----------------------------------------------------
    # 5. Обработчики SocketIO
    # ----------------------------------------------------
    
    # Глобальные словари для отслеживания состояния (должны быть вне create_app для Eventlet)
    # Примечание: в продакшене (Gunicorn/Eventlet) эти словари общие для всех воркеров.
    room_users = {}
    typing_users = {}


    @socketio.on('connect')
    def handle_connect():
        """Обработка подключения нового сокета."""
        if 'user_id' not in session:
            return False 
        # Дополнительная логика подключения
        # print(f"User {session.get('username')} connected.")


    @socketio.on('disconnect')
    def handle_disconnect():
        """Обработка отключения сокета."""
        if 'user_id' not in session:
            return 

        user_id = session.get('user_id')
        username = session.get('username')
        
        rooms_to_leave = [room for room, users in room_users.items() if user_id in users]
        
        for room_name in rooms_to_leave:
            leave_room(room_name)
            
            if user_id in room_users[room_name]:
                del room_users[room_name][user_id]
            
            if room_name in typing_users and user_id in typing_users[room_name]:
                del typing_users[room_name][user_id]
                emit('typing_status', {'user_id': user_id, 'user_name': username, 'is_typing': False}, room=room_name)
            
            emit('status_message', 
                 {'msg': f'{username} покинул(а) комнату.'}, 
                 room=room_name)
                 
                 
    @socketio.on('join')
    def handle_join(data):
        """Обработка присоединения к комнате."""
        if 'user_id' not in session:
            return 
        
        room_name = data.get('room')
        user_id = session.get('user_id')
        username = session.get('username')
        
        if not room_name:
            return

        join_room(room_name)
        
        if room_name not in room_users:
            room_users[room_name] = {}
        room_users[room_name][user_id] = username
        
        # Отправляем историю сообщений
        history = Message.query.filter_by(room_name=room_name).order_by(Message.timestamp.asc()).limit(50).all()
        history_data = [msg.to_dict() for msg in history]
        emit('message_history', 
             {'history': history_data, 'user_name': username},
             room=request.sid)

        # Отправляем системное сообщение
        emit('status_message', 
             {'msg': f'{username} присоединился(ась) к комнате.'}, 
             room=room_name,
             include_self=False)


    @socketio.on('send_message')
    def handle_send_message(data):
        """Обработка отправки нового сообщения."""
        if 'user_id' not in session:
            return 

        user_id = session.get('user_id')
        username = session.get('username')
        content = data.get('content', '').strip()
        
        # Находим комнату, в которой находится пользователь
        room_name = next((r for r in request.rooms if r != request.sid), None)
        
        if not room_name or not content or len(content) > 500:
            emit('error_message', {'msg': 'Сообщение не отправлено. Проверьте длину (макс. 500 симв.).'})
            return

        # 1. Сохранение сообщения в БД
        new_message = Message(
            room_name=room_name,
            user_id=user_id,
            user_name=username,
            content=content,
            timestamp=datetime.utcnow()
        )
        db.session.add(new_message)
        db.session.commit()
        
        # 2. Отправка сообщения всем в комнате
        emit('new_message', new_message.to_dict(), room=room_name)
        
        
    @socketio.on('start_typing')
    def handle_start_typing():
        """Обработка начала печати."""
        if 'user_id' not in session:
            return 

        user_id = session.get('user_id')
        username = session.get('username')
        
        room_name = next((r for r in request.rooms if r != request.sid), None)
        if not room_name:
            return

        if room_name not in typing_users:
            typing_users[room_name] = {}
            
        if user_id not in typing_users[room_name]:
            typing_users[room_name][user_id] = username
            
            emit('typing_status', 
                 {'user_id': user_id, 'user_name': username, 'is_typing': True}, 
                 room=room_name,
                 include_self=False)


    @socketio.on('stop_typing')
    def handle_stop_typing():
        """Обработка окончания печати."""
        if 'user_id' not in session:
            return 

        user_id = session.get('user_id')
        username = session.get('username')
        
        room_name = next((r for r in request.rooms if r != request.sid), None)
        if not room_name:
            return

        if room_name in typing_users and user_id in typing_users[room_name]:
            del typing_users[room_name][user_id]
            
            emit('typing_status', 
                 {'user_id': user_id, 'user_name': username, 'is_typing': False}, 
                 room=room_name,
                 include_self=False)


    return app

# ----------------------------------------------------
# 6. Запуск приложения
# ----------------------------------------------------

# Создаем приложение (для Gunicorn/Eventlet это главный объект приложения)
app = create_app()

# Создаем таблицы БД, если они не существуют
with app.app_context():
    db.create_all()


if __name__ == '__main__':
    # В локальном режиме (dev) можно использовать eventlet.monkey_patch()
    # но для Production Gunicorn это не нужно.
    import eventlet
    eventlet.monkey_patch()
    print("Приложение запущено локально. Используйте http://127.0.0.1:5000/")
    socketio.run(app, debug=True)