import os
# --- КРИТИЧНОЕ ИСПРАВЛЕНИЕ: Eventlet Monkey-Patching ---
# Этот вызов должен быть выполнен ПЕРЕД ВСЕМИ ОСТАЛЬНЫМИ ИМПОРТАМИ,
# чтобы обеспечить асинхронную работу Flask-SocketIO с Eventlet (как указано в Procfile).
import eventlet
eventlet.monkey_patch()
# --------------------------------------------------------

import json
import secrets
from datetime import datetime, timezone

from flask import Flask, render_template, redirect, url_for, request, session, flash
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect, rooms
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

# --- 1. Конфигурация и Инициализация ---
app = Flask(__name__)

# ИСПРАВЛЕНИЕ ДЛЯ RAILWAY (HTTPS):
# Это необходимо, чтобы Flask видел, что запрос пришел по HTTPS и корректно работал с сессиями.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Устанавливаем секретный ключ для защиты сессий
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", secrets.token_hex(16))

# !!! НАСТРОЙКА БАЗЫ ДАННЫХ !!!
database_url = os.getenv("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    # Исправляем формат URL для совместимости с SQLAlchemy и PostgreSQL
    database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)

# Используем PostgreSQL URL, если доступен, иначе SQLite для локальной разработки
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
# Используем eventlet в качестве движка SocketIO, разрешаем CORS для продакшена.
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*") 

# Словарь для отслеживания комнаты, в которой находится каждый пользователь (по user_id)
user_room_map = {} 

# Словарь для отслеживания активных пользователей по комнатам для индикатора печати
typing_users_in_rooms = {} 


# --- 2. Модели Базы Данных ---

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String(36), primary_key=True)  # UUID для user_id
    google_id = db.Column(db.String(128), unique=True, nullable=True)
    name = db.Column(db.String(80), nullable=False)
    # Уникальное имя пользователя в нижнем регистре для проверки уникальности
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
    # Сохраняем время в UTC
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        # Преобразование данных сообщения в словарь для отправки по SocketIO
        # Используем уникальное имя, чтобы оно отображалось в чате
        return {
            'user_id': self.user_id,
            'user_name': self.author.unique_name or self.author.name,
            'content': self.content,
            # Форматируем время для отображения. Обратите внимание: оно будет в UTC.
            'timestamp': self.timestamp.strftime('%H:%M:%S'), 
        }

    def __repr__(self):
        return f'<Message {self.room_name} from {self.user_id}>'


# --- 3. Функции Базы Данных (Удален глобальный вызов setup_database) ---

# Удаляем функцию setup_database() и ее вызов. 
# Логика db.create_all() переносится в роут login, который всегда имеет контекст.
# Это гарантирует, что таблицы создадутся при первом взаимодействии с БД.

# Флаг для однократного создания таблиц
tables_created = False 

def ensure_tables_exist():
    """Проверяет и создает таблицы БД. Вызывается в первом роуте."""
    global tables_created
    if not tables_created:
        try:
            # db.create_all() безопасно, т.к. создает только несуществующие таблицы
            db.create_all() 
            print("Таблицы базы данных созданы (или уже существовали).")
            tables_created = True # Устанавливаем флаг, чтобы не вызывать повторно
        except Exception as e:
            # Если возникла ошибка, скорее всего, проблема с подключением.
            print(f"Критическая ошибка при создании таблиц базы данных: {e}")


# --- 4. Роуты Flask (Аутентификация и Навигация) ---

@app.route('/')
def index():
    if 'user_id' in session:
        # Если пользователь аутентифицирован, показываем страницу выбора комнаты
        return render_template('room_selection.html', user_name=session.get('user_name'))
    # Иначе - страница входа
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    # !!! КРИТИЧЕСКИЙ ВЫЗОВ: Гарантируем, что таблицы существуют в контексте HTTP-запроса
    ensure_tables_exist() 
    
    username = request.form.get('username')

    if not username or not username.strip():
        flash('Имя пользователя не может быть пустым.', 'error')
        return redirect(url_for('index'))
    
    username = username.strip()

    # Проверка на длину
    if len(username) < 3 or len(username) > 30:
        flash('Имя пользователя должно быть от 3 до 30 символов.', 'error')
        return redirect(url_for('index'))
    
    # Нормализуем имя для поиска в базе данных (всегда в нижнем регистре)
    unique_name = username.lower()

    # Роуты Flask уже находятся в контексте приложения
    user = User.query.filter_by(unique_name=unique_name).first()

    if not user:
        # Создаем нового пользователя
        user_id = secrets.token_urlsafe(16)
        # Сохраняем оригинальное имя, но используем уникальное для поиска
        user = User(id=user_id, name=username, unique_name=unique_name) 
        try:
            db.session.add(user)
            db.session.commit()
            print(f"Создан новый пользователь: {username}")
        except Exception as e:
            db.session.rollback()
            # Дополнительная проверка на уникальность, хотя unique=True должен это обрабатывать
            if 'unique_name' in str(e):
                 flash('Это имя пользователя уже занято. Пожалуйста, выберите другое.', 'error')
            else:
                 flash('Произошла ошибка при создании пользователя.', 'error')
            
            print(f"Ошибка при создании пользователя: {e}")
            return redirect(url_for('index'))
    
    # Устанавливаем данные сессии
    session['user_id'] = user.id
    session['user_name'] = user.name
    session['unique_name'] = user.unique_name
    
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    # Проверяем, находится ли пользователь в комнате
    user_id = session.get("user_id")
    if user_id in user_room_map:
        pass 
    
    # Очищаем сессию
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

    room_name = request.form.get('room_name')

    if not room_name or not room_name.strip():
        flash('Имя комнаты не может быть пустым.', 'error')
        return redirect(url_for('index'))
    
    room_name = room_name.strip()
    
    if len(room_name) > 50:
        flash('Имя комнаты слишком длинное.', 'error')
        return redirect(url_for('index'))

    # Редирект на роут комнаты, который отобразит шаблон
    return redirect(url_for('chat_room', room_name=room_name))

@app.route('/chat/<room_name>')
def chat_room(room_name):
    if 'user_id' not in session:
        flash('Сначала войдите в систему.', 'error')
        return redirect(url_for('index'))

    if not room_name:
        return redirect(url_for('index'))

    # Теперь просто отображаем шаблон, логика присоединения будет в SocketIO
    return render_template(
        'chat.html', 
        room_name=room_name, 
        user_id=session['user_id']
    )


# --- 5. SocketIO Обработчики ---

@socketio.on('join')
def on_join(data):
    """Обрабатывает присоединение пользователя к комнате."""
    user_id = session.get("user_id")
    user_name = session.get("user_name")
    room_name = data.get('room')

    if not all([user_id, user_name, room_name]):
        emit('error_message', {'msg': 'Не удалось присоединиться к комнате: отсутствует ID или имя.'})
        return

    # 1. Если пользователь уже в другой комнате, заставляем его покинуть старую
    if user_id in user_room_map and user_room_map[user_id] != room_name:
        old_room = user_room_map[user_id]
        leave_room(old_room)
        # Сообщаем старой комнате об уходе
        emit('status_message', 
             {'msg': f'{user_name} покинул комнату.'}, 
             room=old_room)
        
    # 2. Присоединяем к новой комнате
    join_room(room_name)
    user_room_map[user_id] = room_name # Обновляем карту комнат
    
    # 3. Сообщаем комнате о присоединении нового пользователя
    emit('status_message', 
         {'msg': f'{user_name} присоединился к комнате.'}, 
         room=room_name, 
         include_self=False) # Не отправляем сообщение самому себе

    print(f'Пользователь {user_name} (ID: {user_id[:4]}...) присоединился к комнате {room_name}.')
    
    # 4. Загрузка и отправка истории сообщений
    with app.app_context():
        # Загружаем последние 50 сообщений. SocketIO обработчики требуют контекста для работы с БД!
        messages = Message.query.filter_by(room_name=room_name).order_by(Message.timestamp.desc()).limit(50).all()
        # Разворачиваем список, чтобы сообщения были в хронологическом порядке (сначала старые)
        messages.reverse() 
        
        history = [msg.to_dict() for msg in messages]
        
        # Отправляем историю ТОЛЬКО присоединившемуся пользователю
        emit('message_history', {'history': history, 'user_name': user_name})


@socketio.on('disconnect')
def handle_disconnect():
    """Обрабатывает отключение пользователя."""
    user_id = session.get("user_id")
    user_name = session.get("user_name")
    
    if user_id in user_room_map:
        room_name = user_room_map.pop(user_id)
        
        # Удаляем пользователя из индикатора печати
        if room_name in typing_users_in_rooms and user_id in typing_users_in_rooms[room_name]:
            typing_users_in_rooms[room_name].pop(user_id, None)
            # Оповещаем комнату о прекращении печати (если он печатал)
            emit('typing_status', 
                 {'user_id': user_id, 'user_name': user_name, 'is_typing': False}, 
                 room=room_name, include_self=False)
        
        # Сообщаем всем, что пользователь отключился
        emit('status_message', 
             {'msg': f'{user_name} покинул комнату {room_name}.'}, 
             room=room_name)
        
        print(f'Пользователь {user_name} отключен от комнаты {room_name}.')

@socketio.on('send_message')
def handle_message(data):
    """Обрабатывает отправку нового сообщения."""
    user_id = session.get("user_id")
    room_name = user_room_map.get(user_id)
    user_name = session.get("user_name")
    content = data.get('content', '').strip()

    if not all([user_id, room_name, content, user_name]):
        # Отправляем ошибку только отправителю
        emit('error_message', {'msg': 'Сообщение пустое или произошла ошибка сессии.'})
        return 

    if len(content) > 500: # Ограничение на размер сообщения
        # Отправляем ошибку только отправителю
        emit('error_message', {'msg': 'Сообщение превышает лимит в 500 символов.'})
        # Обрезаем контент перед сохранением
        content = content[:500] 

    # 1. Сохраняем сообщение в базу данных
    with app.app_context():
        # Операции с БД должны быть внутри контекста приложения!
        user = User.query.get(user_id)
        if not user:
            emit('error_message', {'msg': 'Пользователь не найден в базе данных.'})
            return

        new_message = Message(
            room_name=room_name,
            user_id=user_id,
            content=content
        )
        try:
            db.session.add(new_message)
            db.session.commit()
            
            # 2. Отправляем сообщение всем в комнате
            message_data = new_message.to_dict()
            socketio.emit('new_message', message_data, room=room_name)

            print(f'Сообщение в {room_name} от {user_name}: {content[:30]}...')
            
        except Exception as e:
            db.session.rollback()
            print(f"Ошибка базы данных при сохранении сообщения: {e}")
            emit('error_message', {'msg': 'Ошибка сервера при сохранении сообщения.'})
            return


# --- SocketIO: Индикатор Печати ---

@socketio.on('start_typing')
def handle_start_typing():
    """Обрабатывает начало печати."""
    user_id = session.get("user_id")
    user_name = session.get("user_name")
    room_name = user_room_map.get(user_id)
    
    if not all([user_id, room_name]):
        return

    # Добавляем пользователя в список печатающих
    if room_name not in typing_users_in_rooms:
        typing_users_in_rooms[room_name] = {}
        
    # Используем user_id в качестве ключа, user_name в качестве значения
    typing_users_in_rooms[room_name][user_id] = user_name
    
    # Отправляем статус печати ВСЕМ, кроме отправителя
    emit('typing_status', 
         {'user_id': user_id, 'user_name': user_name, 'is_typing': True}, 
         room=room_name, include_self=False)

@socketio.on('stop_typing')
def handle_stop_typing():
    """Обрабатывает прекращение печати."""
    user_id = session.get("user_id")
    room_name = user_room_map.get(user_id)
    
    if not all([user_id, room_name]):
        return
        
    # Удаляем пользователя из списка печатающих
    if room_name in typing_users_in_rooms and user_id in typing_users_in_rooms[room_name]:
        typing_users_in_rooms[room_name].pop(user_id, None)
    
    # Отправляем статус печати ВСЕМ, кроме отправителя (чтобы они обновили UI)
    emit('typing_status', 
         {'user_id': user_id, 'user_name': session['user_name'], 'is_typing': False}, 
         room=room_name, include_self=False)


# --- 6. Запуск приложения ---
if __name__ == '__main__':
    # Если запускается локально (через python main.py), инициализируем таблицы здесь
    with app.app_context():
        try:
            db.create_all()
            print("Локальная инициализация базы данных завершена.")
        except Exception as e:
            print(f"Ошибка локальной инициализации базы данных: {e}")
            
    # Используем socketio.run, который запускает Eventlet
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)