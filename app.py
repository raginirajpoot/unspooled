import sqlite3
import random
import string
import time
import os
from flask import Flask, render_template, request, session, redirect, url_for
from flask_socketio import SocketIO, join_room, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'unspooled-secret-key-dev')
socketio = SocketIO(app, cors_allowed_origins="*")

DB_FILE = 'jam.db'
ADMIN_PASS = os.environ.get('ADMIN_PASSWORD', 'admin123')

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rooms (
                room_code TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                duration INTEGER NOT NULL,
                end_time REAL,
                status TEXT NOT NULL DEFAULT 'waiting',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                author_name TEXT NOT NULL,
                content TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(room_code, author_name),
                FOREIGN KEY (room_code) REFERENCES rooms (room_code)
            )
        ''')
        conn.commit()

init_db()

def generate_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'create':
            admin_key = request.form.get('admin_key')
            if admin_key != ADMIN_PASS:
                return render_template('index.html', error="Invalid Host Passcode", past_rooms=get_finished_rooms())
            
            topic = request.form.get('topic')
            try:
                duration_mins = int(request.form.get('duration', 30))
            except ValueError:
                duration_mins = 30

            room_code = generate_code()
            
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO rooms (room_code, topic, duration, status) VALUES (?, ?, ?, ?)",
                    (room_code, topic, duration_mins * 60, 'waiting')
                )
                conn.commit()

            session['room_code'] = room_code
            session['user_name'] = "Host"
            session['is_admin'] = True
            return redirect(url_for('room', code=room_code))

        elif action == 'join':
            room_code = request.form.get('room_code', '').strip().upper()
            user_name = request.form.get('user_name', '').strip()

            with get_db() as conn:
                room_data = conn.execute("SELECT * FROM rooms WHERE room_code = ?", (room_code,)).fetchone()

            if not room_data:
                return render_template('index.html', error="Sigil not found in the records.", past_rooms=get_finished_rooms())
            if not user_name:
                return render_template('index.html', error="Please state your scribe byline.", past_rooms=get_finished_rooms())

            session['room_code'] = room_code
            session['user_name'] = user_name
            session['is_admin'] = False
            return redirect(url_for('room', code=room_code))

    return render_template('index.html', past_rooms=get_finished_rooms())

def get_finished_rooms():
    with get_db() as conn:
        return conn.execute(
            "SELECT room_code, topic, created_at FROM rooms WHERE status = 'finished' ORDER BY created_at DESC"
        ).fetchall()

@app.route('/room/<code>')
def room(code):
    if 'user_name' not in session:
        return redirect(url_for('index'))
    
    with get_db() as conn:
        room_data = conn.execute("SELECT * FROM rooms WHERE room_code = ?", (code,)).fetchone()
        
    if not room_data:
        return redirect(url_for('index'))

    return render_template('room.html', 
                           code=code, 
                           topic=room_data['topic'], 
                           duration=room_data['duration'],
                           is_admin=session.get('is_admin', False),
                           user_name=session['user_name'])

@socketio.on('join')
def on_join(data):
    code = data['code']
    name = session.get('user_name', 'Anonymous')
    join_room(code)

    with get_db() as conn:
        room_data = conn.execute("SELECT * FROM rooms WHERE room_code = ?", (code,)).fetchone()
        user_draft = conn.execute(
            "SELECT content FROM submissions WHERE room_code = ? AND author_name = ?",
            (code, name)
        ).fetchone()

        all_subs = {}
        if room_data and room_data['status'] == 'finished':
            rows = conn.execute("SELECT author_name, content FROM submissions WHERE room_code = ?", (code,)).fetchall()
            all_subs = {row['author_name']: row['content'] for row in rows}

    if room_data:
        emit('sync_state', {
            'status': room_data['status'],
            'end_time': room_data['end_time'],
            'duration': room_data['duration'],
            'draft': user_draft['content'] if user_draft else '',
            'submissions': all_subs
        })

@socketio.on('start_timer')
def on_start(data):
    code = data['code']
    if session.get('is_admin'):
        with get_db() as conn:
            room_data = conn.execute("SELECT * FROM rooms WHERE room_code = ?", (code,)).fetchone()
            if room_data and room_data['status'] == 'waiting':
                end_time = time.time() + room_data['duration']
                conn.execute("UPDATE rooms SET status = 'running', end_time = ? WHERE room_code = ?", (end_time, code))
                conn.commit()
                emit('timer_started', {
                    'end_time': end_time,
                    'duration': room_data['duration']
                }, to=code)

@socketio.on('save_draft')
def on_save(data):
    code = data['code']
    text = data.get('text', '')
    user = session.get('user_name')

    if not user:
        return

    with get_db() as conn:
        room_data = conn.execute("SELECT status FROM rooms WHERE room_code = ?", (code,)).fetchone()
        if room_data and room_data['status'] == 'running':
            conn.execute('''
                INSERT INTO submissions (room_code, author_name, content, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(room_code, author_name) 
                DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP
            ''', (code, user, text))
            conn.commit()

@socketio.on('finish_session')
def on_finish(data):
    code = data['code']
    with get_db() as conn:
        conn.execute("UPDATE rooms SET status = 'finished' WHERE room_code = ?", (code,))
        conn.commit()
        rows = conn.execute("SELECT author_name, content FROM submissions WHERE room_code = ?", (code,)).fetchall()
        submissions = {row['author_name']: row['content'] for row in rows}

    emit('session_ended', {'submissions': submissions}, to=code)

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)