import os
import hashlib
import sys
import time
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO, emit, join_room
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'savana-secret-2026')
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*", ping_timeout=30, ping_interval=15)
DATABASE_URL = os.environ.get('DATABASE_URL', '')

def get_db():
    for i in range(3):
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            return conn
        except Exception as e:
            print(f"DB connection attempt {i+1} failed: {e}", file=sys.stderr)
            time.sleep(2)
    raise Exception("Cannot connect to database")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL, bio TEXT DEFAULT '', last_seen TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS chats (
        id SERIAL PRIMARY KEY, type TEXT NOT NULL, name TEXT,
        description TEXT, created_by INTEGER, created_at TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        chat_id INTEGER, user_id INTEGER, role TEXT DEFAULT 'member',
        joined_at TIMESTAMP DEFAULT NOW(), PRIMARY KEY(chat_id, user_id))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        content TEXT NOT NULL, reply_to INTEGER DEFAULT NULL,
        edited INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS reactions (
        id SERIAL PRIMARY KEY, msg_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        emoji TEXT NOT NULL, UNIQUE(msg_id, user_id))''')
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"DB init error: {e}", file=sys.stderr)

def hash_pwd(p):
    return hashlib.sha256(p.encode()).hexdigest()

def get_user():
    if 'uid' not in session:
        return None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE id=%s', (session['uid'],))
        u = cur.fetchone()
        cur.close()
        conn.close()
        return u
    except:
        return None

@app.route('/')
def index():
    user = get_user()
    if not user:
        return redirect('/login')
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            SELECT c.id, c.type, c.name, c.description, c.created_at,
            (SELECT content FROM messages WHERE chat_id=c.id AND deleted=0 ORDER BY id DESC LIMIT 1) as last_msg,
            (SELECT u.username FROM messages m JOIN users u ON m.user_id=u.id WHERE m.chat_id=c.id AND m.deleted=0 ORDER BY m.id DESC LIMIT 1) as last_msg_user,
            (SELECT created_at FROM messages WHERE chat_id=c.id AND deleted=0 ORDER BY id DESC LIMIT 1) as last_msg_time
            FROM chats c JOIN chat_members cm ON c.id=cm.chat_id
            WHERE cm.user_id=%s GROUP BY c.id ORDER BY last_msg_time DESC NULLS LAST, c.id DESC
        ''', (user['id'],))
        chats = cur.fetchall()
        for ch in chats:
            if ch.get('last_msg_time') and not isinstance(ch['last_msg_time'], str):
                ch['last_msg_time'] = ch['last_msg_time'].strftime('%Y-%m-%d %H:%M:%S')
        cur.close()
        conn.close()
        return render_template('index.html', user=user, chats=chats)
    except Exception as e:
        import traceback
        return f"<h1>Ошибка</h1><pre>{traceback.format_exc()}</pre>", 500

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        if not u or not p:
            return render_template('index.html', error='Заполни все поля', login=True, user=None, chats=[])
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('SELECT * FROM users WHERE username=%s AND password=%s', (u, hash_pwd(p)))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                session['uid'] = row['id']
                return redirect('/')
            return render_template('index.html', error='Неверный логин или пароль', login=True, user=None, chats=[])
        except Exception as e:
            return render_template('index.html', error=f'Ошибка: {e}', login=True, user=None, chats=[])
    return render_template('index.html', login=True, user=None, chats=[])

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        if not u or not p:
            return render_template('index.html', error='Заполни все поля', register=True, user=None, chats=[])
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('INSERT INTO users (username, password) VALUES (%s,%s) RETURNING id', (u, hash_pwd(p)))
            row = cur.fetchone()
            conn.commit()
            cur.close()
            conn.close()
            session['uid'] = row['id']
            return redirect('/')
        except psycopg2.errors.UniqueViolation:
            return render_template('index.html', error='Имя уже занято', register=True, user=None, chats=[])
        except Exception as e:
            return render_template('index.html', error=f'Ошибка: {e}', register=True, user=None, chats=[])
    return render_template('index.html', register=True, user=None, chats=[])

@app.route('/logout')
def logout():
    session.pop('uid', None)
    return redirect('/login')

@app.route('/api/search')
def api_search():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    q = request.args.get('q', '').strip()
    if not q: return jsonify({'users': [], 'chats': []})
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT id, username, last_seen FROM users WHERE username ILIKE %s AND id!=%s LIMIT 10', (f'%{q}%', user['id']))
        users = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id, name, type FROM chats WHERE type != 'private' AND name ILIKE %s LIMIT 5", (f'%{q}%',))
        chats = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify({'users': users, 'chats': chats})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chats')
def api_chats():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            SELECT c.id, c.type, c.name, c.description, c.created_at,
            (SELECT content FROM messages WHERE chat_id=c.id AND deleted=0 ORDER BY id DESC LIMIT 1) as last_msg,
            (SELECT u.username FROM messages m JOIN users u ON m.user_id=u.id WHERE m.chat_id=c.id AND m.deleted=0 ORDER BY m.id DESC LIMIT 1) as last_msg_user,
            (SELECT created_at FROM messages WHERE chat_id=c.id AND deleted=0 ORDER BY id DESC LIMIT 1) as last_msg_time
            FROM chats c JOIN chat_members cm ON c.id=cm.chat_id
            WHERE cm.user_id=%s ORDER BY last_msg_time DESC NULLS LAST, c.id DESC
        ''', (user['id'],))
        chats = cur.fetchall()
        for ch in chats:
            if ch.get('last_msg_time') and not isinstance(ch['last_msg_time'], str):
                ch['last_msg_time'] = ch['last_msg_time'].strftime('%Y-%m-%d %H:%M:%S')
        cur.close(); conn.close()
        return jsonify({'chats': chats})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat/<int:cid>')
def api_chat(cid):
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM chat_members WHERE chat_id=%s AND user_id=%s', (cid, user['id']))
        if not cur.fetchone(): return jsonify({'error': 'access'}), 403
        
        cur.execute('SELECT * FROM chats WHERE id=%s', (cid,))
        chat = dict(cur.fetchone())
        
        cur.execute('''SELECT u.id, u.username, u.last_seen, cm.role
            FROM users u JOIN chat_members cm ON u.id=cm.user_id
            WHERE cm.chat_id=%s ORDER BY cm.role DESC, u.username''', (cid,))
        members = [dict(r) for r in cur.fetchall()]
        
        cur.execute('''SELECT m.*, u.username,
            (SELECT content FROM messages WHERE id=m.reply_to) as reply_content,
            (SELECT u2.username FROM messages m2 JOIN users u2 ON m2.user_id=u2.id WHERE m2.id=m.reply_to) as reply_user
            FROM messages m JOIN users u ON m.user_id=u.id
            WHERE m.chat_id=%s ORDER BY m.id DESC LIMIT 100''', (cid,))
        msgs = cur.fetchall()
        msg_ids = [m['id'] for m in msgs]
        
        reactions = {}
        if msg_ids:
            cur.execute('''SELECT msg_id, emoji, COUNT(*) as cnt, STRING_AGG(user_id::text, ',') as user_ids
                FROM reactions WHERE msg_id = ANY(%s) GROUP BY msg_id, emoji''', (msg_ids,))
            for r in cur.fetchall():
                if r['msg_id'] not in reactions: reactions[r['msg_id']] = []
                reactions[r['msg_id']].append({
                    'emoji': r['emoji'], 'cnt': r['cnt'],
                    'mine': str(user['id']) in (r['user_ids'] or '').split(',')
                })
                
        msgs_list = []
        for m in reversed(msgs):
            md = dict(m)
            md['reactions'] = reactions.get(m['id'], [])
            if md.get('created_at'): md['created_at'] = str(md['created_at'])
            msgs_list.append(md)
            
        cur.close(); conn.close()
        return jsonify({'chat': chat, 'members': members, 'messages': msgs_list, 'my_uid': user['id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/create_chat', methods=['POST'])
def api_create_chat():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    data = request.json or {}
    ctype = data.get('type', 'private')
    name = data.get('name', '').strip()
    desc = data.get('desc', '').strip()
    target_uid = data.get('target_uid')
    try:
        conn = get_db()
        cur = conn.cursor()
        if ctype == 'private' and target_uid:
            cur.execute('''SELECT c.id FROM chats c
                JOIN chat_members m1 ON c.id=m1.chat_id AND m1.user_id=%s
                JOIN chat_members m2 ON c.id=m2.chat_id AND m2.user_id=%s
                WHERE c.type='private' GROUP BY c.id HAVING COUNT(*)=2''', (user['id'], target_uid))
            existing = cur.fetchone()
            if existing: return jsonify({'ok': True, 'id': existing['id']})
            cur.execute('INSERT INTO chats (type, name, description, created_by) VALUES (%s,%s,%s,%s) RETURNING id', ('private', None, None, user['id']))
            cid = cur.fetchone()['id']
            cur.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (%s,%s,%s)', (cid, user['id'], 'owner'))
            cur.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (%s,%s,%s)', (cid, target_uid, 'member'))
        else:
            if not name: return jsonify({'error': 'name'}), 400
            cur.execute('INSERT INTO chats (type, name, description, created_by) VALUES (%s,%s,%s,%s) RETURNING id', (ctype, name, desc, user['id']))
            cid = cur.fetchone()['id']
            cur.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (%s,%s,%s)', (cid, user['id'], 'owner'))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True, 'id': cid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/profile', methods=['GET', 'POST'])
def api_profile():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    if request.method == 'GET':
        return jsonify({'id': user['id'], 'username': user['username'], 'bio': user.get('bio', '')})
    data = request.json or {}
    bio = data.get('bio', '').strip()[:200]
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('UPDATE users SET bio=%s WHERE id=%s', (bio, user['id']))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@socketio.on('connect')
def on_connect(): pass

@socketio.on('join')
def on_join(data):
    cid = data.get('cid')
    if cid: join_room(f'chat_{cid}')

@socketio.on('msg')
def on_msg(data):
    user = get_user()
    if not user: return
    cid = data.get('cid'); content = data.get('content', '').strip(); reply_to = data.get('reply_to')
    if not cid or not content: return
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT 1 FROM chat_members WHERE chat_id=%s AND user_id=%s', (cid, user['id']))
    if not cur.fetchone(): return
    cur.execute('INSERT INTO messages (chat_id, user_id, content, reply_to) VALUES (%s,%s,%s,%s) RETURNING id', (cid, user['id'], content, reply_to))
    mid = cur.fetchone()['id']
    reply_content = None; reply_user = None
    if reply_to:
        cur.execute('SELECT m.content, u.username FROM messages m JOIN users u ON m.user_id=u.id WHERE m.id=%s', (reply_to,))
        rm = cur.fetchone()
        if rm: reply_content, reply_user = rm['content'], rm['username']
    conn.commit(); cur.close(); conn.close()
    emit('msg', {
        'id': mid, 'cid': cid, 'uid': user['id'], 'user': user['username'],
        'text': content, 'time': datetime.now().strftime('%H:%M'),
        'reply_to': reply_to, 'reply_content': reply_content, 'reply_user': reply_user,
        'edited': 0, 'deleted': 0
    }, broadcast=True)

@socketio.on('typing')
def on_typing(data):
    user = get_user()
    if not user: return
    cid = data.get('cid')
    if cid: emit('typing', {'user': user['username'], 'cid': cid}, room=f'chat_{cid}', include_self=False)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
