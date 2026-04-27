import os, hashlib, sys, time
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO, emit, join_room
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'savana-2026')
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*', ping_timeout=30, ping_interval=15)

DATABASE_URL = os.environ.get('DATABASE_URL', '')

def get_db():
    for i in range(3):
        try: return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        except Exception as e: print(f'DB attempt {i+1}: {e}', file=sys.stderr); time.sleep(2)
    raise Exception('Cannot connect to DB')

def init_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL, bio TEXT DEFAULT '', last_seen TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS chats (
        id SERIAL PRIMARY KEY, type TEXT NOT NULL, name TEXT,
        description TEXT, created_by INTEGER, created_at TIMESTAMP DEFAULT NOW())''')
    cur.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        chat_id INTEGER, user_id INTEGER, role TEXT DEFAULT 'member',
        PRIMARY KEY(chat_id, user_id))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        content TEXT NOT NULL, reply_to INTEGER DEFAULT NULL,
        edited BOOLEAN DEFAULT FALSE, deleted BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT NOW())''')
    conn.commit(); cur.close(); conn.close()

try: init_db()
except Exception as e: print(f'DB init error: {e}', file=sys.stderr)

def hash_pwd(p): return hashlib.sha256(p.encode()).hexdigest()

def get_user():
    if 'uid' not in session: return None
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE id=%s', (session['uid'],))
        u = cur.fetchone(); cur.close(); conn.close(); return u
    except: return None

def update_seen(uid):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('UPDATE users SET last_seen=NOW() WHERE id=%s', (uid,))
        conn.commit(); cur.close(); conn.close()
    except: pass

# ── PAGES ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    user = get_user()
    if not user: return redirect('/login')
    update_seen(user['id'])
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('''
            SELECT c.id, c.type, c.name,
                (SELECT content FROM messages WHERE chat_id=c.id AND deleted=FALSE ORDER BY id DESC LIMIT 1) as last_msg,
                (SELECT u.username FROM messages m JOIN users u ON m.user_id=u.id WHERE m.chat_id=c.id AND m.deleted=FALSE ORDER BY m.id DESC LIMIT 1) as last_msg_user,
                (SELECT created_at FROM messages WHERE chat_id=c.id AND deleted=FALSE ORDER BY id DESC LIMIT 1) as last_msg_time
            FROM chats c JOIN chat_members cm ON c.id=cm.chat_id
            WHERE cm.user_id=%s GROUP BY c.id ORDER BY last_msg_time DESC NULLS LAST
        ''', (user['id'],))
        chats = cur.fetchall(); cur.close(); conn.close()
        for ch in chats:
            if ch.get('last_msg_time') and not isinstance(ch['last_msg_time'], str):
                ch['last_msg_time'] = ch['last_msg_time'].strftime('%H:%M')
        return render_template('index.html', user=user, chats=chats)
    except Exception as e:
        import traceback; return f'<pre>{traceback.format_exc()}</pre>', 500

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','')
        if not u or not p: return render_template('index.html', error='Заполни все поля', page='login', user=None, chats=[])
        try:
            conn = get_db(); cur = conn.cursor()
            cur.execute('SELECT * FROM users WHERE username=%s AND password=%s', (u, hash_pwd(p)))
            row = cur.fetchone(); cur.close(); conn.close()
            if row: session['uid'] = row['id']; update_seen(row['id']); return redirect('/')
            return render_template('index.html', error='Неверный логин или пароль', page='login', user=None, chats=[])
        except Exception as e: return render_template('index.html', error=str(e), page='login', user=None, chats=[])
    return render_template('index.html', page='login', user=None, chats=[])

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','')
        if not u or not p: return render_template('index.html', error='Заполни все поля', page='register', user=None, chats=[])
        try:
            conn = get_db(); cur = conn.cursor()
            cur.execute('INSERT INTO users (username, password) VALUES (%s,%s) RETURNING id', (u, hash_pwd(p)))
            row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
            session['uid'] = row['id']; return redirect('/')
        except psycopg2.errors.UniqueViolation:
            return render_template('index.html', error='Имя уже занято', page='register', user=None, chats=[])
        except Exception as e: return render_template('index.html', error=str(e), page='register', user=None, chats=[])
    return render_template('index.html', page='register', user=None, chats=[])

@app.route('/logout')
def logout():
    session.pop('uid', None); return redirect('/login')

# ── API ──────────────────────────────────────────────────────────────────────

@app.route('/api/search')
def api_search():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    q = request.args.get('q','').strip()
    if not q: return jsonify({'users': [], 'chats': []})
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT id, username, last_seen FROM users WHERE username ILIKE %s AND id!=%s LIMIT 10', (f'%{q}%', user['id']))
        users = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get('last_seen'): d['last_seen'] = d['last_seen'].isoformat()
            users.append(d)
        cur.execute("SELECT id, name, type FROM chats WHERE type!='private' AND name ILIKE %s LIMIT 5", (f'%{q}%',))
        chats = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify({'users': users, 'chats': chats})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/chat/<int:cid>')
def api_chat(cid):
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT type FROM chats WHERE id=%s', (cid,))
        ch = cur.fetchone()
        if not ch: return jsonify({'error': 'not found'}), 404
        cur.execute('SELECT 1 FROM chat_members WHERE chat_id=%s AND user_id=%s', (cid, user['id']))
        if not cur.fetchone():
            if ch['type'] != 'private':
                cur.execute('INSERT INTO chat_members (chat_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (cid, user['id']))
                conn.commit()
            else: cur.close(); conn.close(); return jsonify({'error': 'access'}), 403
        cur.execute('SELECT * FROM chats WHERE id=%s', (cid,))
        chat = dict(cur.fetchone())
        if chat.get('created_at'): chat['created_at'] = str(chat['created_at'])
        cur.execute('''SELECT u.id, u.username, u.last_seen, cm.role
            FROM users u JOIN chat_members cm ON u.id=cm.user_id WHERE cm.chat_id=%s ORDER BY cm.role DESC''', (cid,))
        members = []
        for m in cur.fetchall():
            d = dict(m)
            if d.get('last_seen'): d['last_seen'] = d['last_seen'].isoformat()
            members.append(d)
        cur.execute('''SELECT m.id, m.chat_id, m.user_id, m.content, m.reply_to,
            m.edited, m.deleted, m.created_at, u.username,
            (SELECT content FROM messages WHERE id=m.reply_to) as reply_content,
            (SELECT u2.username FROM messages m2 JOIN users u2 ON m2.user_id=u2.id WHERE m2.id=m.reply_to) as reply_user
            FROM messages m JOIN users u ON m.user_id=u.id
            WHERE m.chat_id=%s ORDER BY m.id DESC LIMIT 100''', (cid,))
        msgs = []
        for m in reversed(cur.fetchall()):
            d = dict(m)
            if d.get('created_at'): d['created_at'] = str(d['created_at'])[:16]
            msgs.append(d)
        cur.close(); conn.close()
        return jsonify({'chat': chat, 'members': members, 'messages': msgs, 'my_uid': user['id']})
    except Exception as e:
        import traceback; return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

@app.route('/api/create_chat', methods=['POST'])
def api_create_chat():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    data = request.json or {}
    ctype = data.get('type', 'group')
    name = data.get('name','').strip()
    target_uid = data.get('target_uid')
    try:
        conn = get_db(); cur = conn.cursor()
        if ctype == 'private' and target_uid:
            cur.execute('''SELECT c.id FROM chats c
                JOIN chat_members m1 ON c.id=m1.chat_id AND m1.user_id=%s
                JOIN chat_members m2 ON c.id=m2.chat_id AND m2.user_id=%s
                WHERE c.type='private' GROUP BY c.id HAVING COUNT(*)=2''', (user['id'], target_uid))
            ex = cur.fetchone()
            if ex: cur.close(); conn.close(); return jsonify({'ok': True, 'id': ex['id']})
            cur.execute("INSERT INTO chats (type,name,created_by) VALUES ('private',NULL,%s) RETURNING id", (user['id'],))
            cid = cur.fetchone()['id']
            cur.execute('INSERT INTO chat_members (chat_id,user_id,role) VALUES (%s,%s,%s)', (cid, user['id'], 'owner'))
            cur.execute('INSERT INTO chat_members (chat_id,user_id,role) VALUES (%s,%s,%s)', (cid, target_uid, 'member'))
        else:
            if not name: return jsonify({'error': 'Введи название'}), 400
            cur.execute('INSERT INTO chats (type,name,created_by) VALUES (%s,%s,%s) RETURNING id', (ctype, name, user['id']))
            cid = cur.fetchone()['id']
            cur.execute('INSERT INTO chat_members (chat_id,user_id,role) VALUES (%s,%s,%s)', (cid, user['id'], 'owner'))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True, 'id': cid})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/invite', methods=['POST'])
def api_invite():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    data = request.json or {}
    cid = data.get('chat_id'); target_uid = data.get('target_uid')
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('INSERT INTO chat_members (chat_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (cid, target_uid))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/msg/delete', methods=['POST'])
def api_delete_msg():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    mid = (request.json or {}).get('id')
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT * FROM messages WHERE id=%s AND user_id=%s', (mid, user['id']))
        msg = cur.fetchone()
        if not msg: cur.close(); conn.close(); return jsonify({'error': 'not found'}), 404
        cur.execute("UPDATE messages SET deleted=TRUE, content='Сообщение удалено' WHERE id=%s", (mid,))
        conn.commit(); cur.close(); conn.close()
        socketio.emit('msg_deleted', {'id': mid, 'cid': msg['chat_id']}, room=f'chat_{msg["chat_id"]}')
        return jsonify({'ok': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/msg/edit', methods=['POST'])
def api_edit_msg():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    data = request.json or {}; mid = data.get('id'); content = data.get('content','').strip()
    if not content: return jsonify({'error': 'empty'}), 400
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT * FROM messages WHERE id=%s AND user_id=%s', (mid, user['id']))
        msg = cur.fetchone()
        if not msg: cur.close(); conn.close(); return jsonify({'error': 'not found'}), 404
        cur.execute('UPDATE messages SET content=%s, edited=TRUE WHERE id=%s', (content, mid))
        conn.commit(); cur.close(); conn.close()
        socketio.emit('msg_edited', {'id': mid, 'content': content, 'cid': msg['chat_id']}, room=f'chat_{msg["chat_id"]}')
        return jsonify({'ok': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/chat/clear', methods=['POST'])
def api_clear_chat():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    cid = (request.json or {}).get('chat_id')
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT 1 FROM chat_members WHERE chat_id=%s AND user_id=%s', (cid, user['id']))
        if not cur.fetchone(): cur.close(); conn.close(); return jsonify({'error': 'access'}), 403
        cur.execute("UPDATE messages SET deleted=TRUE, content='Удалено' WHERE chat_id=%s", (cid,))
        conn.commit(); cur.close(); conn.close()
        socketio.emit('chat_cleared', {'cid': cid}, room=f'chat_{cid}')
        return jsonify({'ok': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/profile', methods=['POST'])
def api_profile():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    data = request.json or {}
    bio = data.get('bio','').strip()[:200]
    new_username = data.get('username','').strip()
    try:
        conn = get_db(); cur = conn.cursor()
        if new_username and new_username != user['username']:
            cur.execute('SELECT id FROM users WHERE username=%s', (new_username,))
            if cur.fetchone(): cur.close(); conn.close(); return jsonify({'error': 'Имя занято'}), 400
            cur.execute('UPDATE users SET bio=%s, username=%s WHERE id=%s', (bio, new_username, user['id']))
        else:
            cur.execute('UPDATE users SET bio=%s WHERE id=%s', (bio, user['id']))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True})
    except Exception as e: return jsonify({'error': str(e)}), 500

# ── SOCKETS ──────────────────────────────────────────────────────────────────

@socketio.on('join')
def on_join(data):
    cid = data.get('cid')
    if cid: join_room(f'chat_{cid}')

@socketio.on('msg')
def on_msg(data):
    user = get_user()
    if not user: return
    cid = data.get('cid'); content = data.get('content','').strip(); reply_to = data.get('reply_to')
    if not cid or not content: return
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT 1 FROM chat_members WHERE chat_id=%s AND user_id=%s', (cid, user['id']))
        if not cur.fetchone(): cur.close(); conn.close(); return
        cur.execute('INSERT INTO messages (chat_id,user_id,content,reply_to) VALUES (%s,%s,%s,%s) RETURNING id',
                    (cid, user['id'], content, reply_to))
        mid = cur.fetchone()['id']
        reply_content = reply_user_name = None
        if reply_to:
            cur.execute('SELECT m.content, u.username FROM messages m JOIN users u ON m.user_id=u.id WHERE m.id=%s', (reply_to,))
            rm = cur.fetchone()
            if rm: reply_content = rm['content']; reply_user_name = rm['username']
        conn.commit(); cur.close(); conn.close()
        update_seen(user['id'])
        emit('msg', {
            'id': mid, 'cid': cid, 'uid': user['id'], 'user': user['username'],
            'text': content, 'time': datetime.now().strftime('%H:%M'),
            'reply_to': reply_to, 'reply_content': reply_content, 'reply_user': reply_user_name,
            'edited': False, 'deleted': False
        }, room=f'chat_{cid}')
    except Exception as e: print(f'Socket msg error: {e}', file=sys.stderr)

@socketio.on('typing')
def on_typing(data):
    user = get_user()
    if not user: return
    cid = data.get('cid')
    if cid: emit('typing', {'user': user['username'], 'cid': cid}, room=f'chat_{cid}', include_self=False)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
