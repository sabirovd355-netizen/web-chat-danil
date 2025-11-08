import os
import eventlet
# КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Eventlet Monkey-Patching 
# Этот вызов должен быть выполнен ПЕРЕД ВСЕМИ ОСТАЛЬНЫМИ ИМПОРТАМИ,
# чтобы обеспечить асинхронную работу Flask-SocketIO с Eventlet.
eventlet.monkey_patch() 

import json
import secrets
from datetime import datetime, timezone

# Удаление неиспользуемых импортов requests и oauthlib для чистоты
from flask import Flask, render_template, redirect, url_for, request, session, flash
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect, rooms
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

# 1. Глобальные, НЕПРИВЯЗАННЫЕ объекты
# SQLAlchemy и SocketIO инициализируются без привязки к приложению,
# чтобы избежать ошибки контекста при импорте.
db = SQLAlchemy()
socketio = SocketIO(cors_allowed_origins="*") # CORS для веб-сокетов

# 2. Глобальные словари для отслеживания состояния (должны быть вне create_app для Eventlet)
user_room_map = {}
typing_users_in_rooms = {}

def create_app():
    # Конфигурация и Инициализация
    app = Flask(__name__)
    
    # ИСПРАВЛЕНИЕ ДЛЯ RAILWAY (HTTPS):
    # Это необходимо, чтобы Flask видел, что запрос пришел по HTTPS и корректно работал с сессиями.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    
    # ----------------------------------------------------
    # КОНФИГУРАЦИЯ
    # ----------------------------------------------------
    
    # Secret Key (из переменной окружения Railway)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "your_fallback_secret_key_dev")

    # Конфигурация БД (из переменной окружения Railway)
    # Используем DATABASE_URL от Railway
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Инициализация
    db.init_app(app)
    socketio.init_app(app)

    # --- ЗДЕСЬ ДОЛЖЕН БЫТЬ ВАШ КОД GOOGLE OAUTH И ЛЮБЫЕ ВАШИ РОУТЫ (@app.route) ---
    # ...

    # 3. Модели
    class User(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        username = db.Column(db.String(80), unique=True, nullable=False)
        google_id = db.Column(db.String(120), unique=True, nullable=True)
        # Добавьте другие поля по необходимости

        def __repr__(self):
            return f'<User {self.username}>'

    class Message(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        room = db.Column(db.String(80), nullable=False)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
        text = db.Column(db.Text, nullable=False)
        # Убедитесь, что timestamp сохраняется в UTC, как требует Heroku/Railway
        timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc)) 
        
        user = db.relationship('User', backref=db.backref('messages', lazy=True))

        def to_dict(self):
            return {
                'username': self.user.username,
                'text': self.text,
                # Форматируем для отображения
                'timestamp': self.timestamp.astimezone(timezone.utc).strftime('%H:%M:%S'),
                'is_admin': self.user.username.lower() == 'admin' # Пример логики для администратора
            }

    # 4. Создание таблиц БД
    # В реальном приложении это лучше делать через миграции Alembic,
    # но для простоты развертывания на Railway можно использовать этот метод.
    with app.app_context():
        # Если база данных не инициализирована (например, при первом развертывании)
        try:
            db.create_all()
        except Exception as e:
            # При повторных запусках это может вызвать предупреждение/ошибку, которую можно игнорировать
            print(f"Database tables might already exist: {e}")

    # ----------------------------------------------------
    # АВТОРИЗАЦИЯ GOOGLE OAUTH (пример)
    # ----------------------------------------------------
    
    # Здесь должен быть ваш код для OAUTH
    # @app.route('/login') ...
    # @app.route('/callback') ...
    
    # ПРИМЕР РОУТИНГА
    @app.route('/')
    def index():
        if 'user_id' in session:
            # Замените 'index.html' на ваш шаблон
            return render_template('index.html', username=User.query.get(session['user_id']).username) 
        return redirect(url_for('login')) 

    @app.route('/login')
    def login():
        # Замените 'login.html' на ваш шаблон
        return render_template('login.html') 

    # ----------------------------------------------------
    # ОБРАБОТЧИКИ SOCKETIO
    # ----------------------------------------------------
    
    @socketio.on('join')
    def on_join(data):
        room = data.get('room')
        user_id = session.get('user_id')
        
        if not user_id or not room:
            return

        with app.app_context():
            user = User.query.get(user_id)
            if not user:
                return
                
            username = user.username
            join_room(room)
            user_room_map[request.sid] = room
            
            # Отправка истории сообщений
            messages = Message.query.filter_by(room=room).order_by(Message.timestamp.desc()).limit(50).all()
            # Отправляем в обратном порядке, чтобы новые были внизу
            messages_data = [msg.to_dict() for msg in reversed(messages)]
            emit('message_history', messages_data, room=request.sid)

            # Сообщаем всем в комнате
            emit('status', {'msg': f'{username} присоединился к комнате {room}.'}, room=room)
            
            # Обновление списка активных пользователей
            # update_active_users(room) # Используйте, когда будет полностью реализована

    @socketio.on('text')
    def on_text(data):
        room = user_room_map.get(request.sid)
        user_id = session.get('user_id')
        text = data.get('msg')
        
        if not room or not user_id or not text:
            return

        with app.app_context():
            user = User.query.get(user_id)
            if not user:
                return

            # Сохранение сообщения в БД
            new_message = Message(room=room, user=user, text=text)
            db.session.add(new_message)
            db.session.commit()
            
            # Отправка сообщения всем в комнате
            emit('message', new_message.to_dict(), room=room)

    @socketio.on('typing')
    def on_typing(data):
        room = user_room_map.get(request.sid)
        user_id = session.get('user_id')
        is_typing = data.get('is_typing', False)
        
        if not room or not user_id:
            return

        with app.app_context():
            user = User.query.get(user_id)
            if not user:
                return
            
            username = user.username
            
            # Обновление состояния печатающих пользователей
            if room not in typing_users_in_rooms:
                typing_users_in_rooms[room] = set()

            if is_typing:
                typing_users_in_rooms[room].add(username)
            else:
                typing_users_in_rooms[room].discard(username)

            # Отправка обновленного списка печатающих пользователей
            typing_list = list(typing_users_in_rooms[room])
            emit('typing_status', {'typing_users': typing_list}, room=room, include_self=False)


    @socketio.on('leave')
    def on_leave(data):
        room = data.get('room')
        user_id = session.get('user_id')
        
        if not user_id or not room:
            return

        with app.app_context():
            user = User.query.get(user_id)
            if not user:
                return
                
            username = user.username
            
            leave_room(room)
            if request.sid in user_room_map:
                del user_room_map[request.sid]
            
            # Сообщаем всем в комнате
            emit('status', {'msg': f'{username} покинул комнату {room}.'}, room=room)

            # Обновление списка активных пользователей
            # update_active_users(room) # Используйте, когда будет полностью реализована


    @socketio.on('disconnect')
    def on_disconnect():
        # Находим комнату, в которой был пользователь
        room = user_room_map.pop(request.sid, None)
        user_id = session.get('user_id')
        
        if not room or not user_id:
            return

        with app.app_context():
            user = User.query.get(user_id)
            if not user:
                return
                
            username = user.username
            
            # Удаление из списка печатающих
            if room in typing_users_in_rooms:
                typing_users_in_rooms[room].discard(username)
                
            # Сообщаем всем в комнате (которую он покинул)
            emit('status', {'msg': f'{username} отключился.'}, room=room)
            
            # Обновление списка активных пользователей
            # update_active_users(room) # Используйте, когда будет полностью реализована

    
    # ----------------------------------------------------
    # ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (пока не полностью реализованы)
    # ----------------------------------------------------
    
    def update_active_users(room):
        # Эта функция требует надежного способа маппинга SID -> User, 
        # который выходит за рамки простой сессии Flask.
        pass

    return app

# Запуск приложения
app = create_app()

if __name__ == '__main__':
    # Локальный запуск (этот код не будет работать на Railway)
    socketio.run(app, debug=True)

# Точка входа для Gunicorn/Railway: 
# Gunicorn будет искать переменную 'app' в этом файле.
# gunicorn --worker-class eventlet -w 1 main:app