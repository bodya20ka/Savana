import eventlet
eventlet.monkey_patch()

import os
import sqlite3
import hashlib
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, jsonify
from flask_socketio import SocketIO, emit, join_room

DB_PATH = os.environ.get('DB_PATH', 'savana.db')

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT,
            description TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id INTEGER,
            user_id INTEGER,
            role TEXT DEFAULT 'member',
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    conn.close()
    print("✅ БД готова")

def hash_pwd(p): return hashlib.sha256(p.encode()).hexdigest()

def get_user():
    if 'uid' in session:
        try:
            conn = get_db()
            u = conn.execute('SELECT * FROM users WHERE id=?', (session['uid'],)).fetchone()
            conn.close()
            return u
        except: return None
    return None

def update_seen(uid):
    try:
        conn = get_db()
        conn.execute('UPDATE users SET last_seen=CURRENT_TIMESTAMP WHERE id=?', (uid,))
        conn.commit()
        conn.close()
    except: pass

@app.route('/')
def index():
    user = get_user()
    if not user: return redirect('/login')
    update_seen(user['id'])
    try:
        conn = get_db()
        chats = conn.execute('''
            SELECT c.*, 
                   (SELECT COUNT(*) FROM chat_members WHERE chat_id=c.id) as members_count,
                   (SELECT content FROM messages WHERE chat_id=c.id ORDER BY id DESC LIMIT 1) as last_msg,
                   (SELECT created_at FROM messages WHERE chat_id=c.id ORDER BY id DESC LIMIT 1) as last_msg_time
            FROM chats c
            JOIN chat_members cm ON c.id=cm.chat_id
            WHERE cm.user_id=?
            GROUP BY c.id
            ORDER BY c.id DESC
        ''', (user['id'],)).fetchall()
        conn.close()
        return render_template('index.html', user=user, chats=chats)
    except Exception as e:
        print(f"❌ / index error: {e}")
        return "Ошибка загрузки чатов", 500

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        if not u or not p:
            return render_template('index.html', error='Заполни все поля', login=True, user=None, chats=[])
        try:
            conn = get_db()
            row = conn.execute('SELECT * FROM users WHERE username=? AND password=?', (u, hash_pwd(p))).fetchone()
            conn.close()
            if row:
                session['uid'] = row['id']
                update_seen(row['id'])
                return redirect('/')
            return render_template('index.html', error='Неверный логин или пароль', login=True, user=None, chats=[])
        except Exception as e:
            print(f"❌ Login error: {e}")
            return render_template('index.html', error='Ошибка сервера', login=True, user=None, chats=[])
    return render_template('index.html', login=True, user=None, chats=[])

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        if not u or not p:
            return render_template('index.html', error='Заполни все поля', register=True, user=None, chats=[])
        try:
            conn = get_db()
            conn.execute('INSERT INTO users (username, password) VALUES (?,?)', (u, hash_pwd(p)))
            conn.commit()
            row = conn.execute('SELECT * FROM users WHERE username=?', (u,)).fetchone()
            conn.close()
            session['uid'] = row['id']
            update_seen(row['id'])
            return redirect('/')
        except sqlite3.IntegrityError:
            return render_template('index.html', error='Это имя уже занято', register=True, user=None, chats=[])
        except Exception as e:
            print(f"❌ Register error: {e}")
            return render_template('index.html', error='Ошибка регистрации', register=True, user=None, chats=[])
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
    conn = get_db()
    users = conn.execute('SELECT id, username, last_seen FROM users WHERE username LIKE ? AND id!=? LIMIT 8', (f'%{q}%', user['id'])).fetchall()
    chats = conn.execute('''
        SELECT c.id, c.name, c.type, c.description
        FROM chats c JOIN chat_members cm ON c.id=cm.chat_id
        WHERE cm.user_id=? AND c.name LIKE ? LIMIT 5
    ''', (user['id'], f'%{q}%')).fetchall()
    conn.close()
    return jsonify({'users': [dict(u) for u in users], 'chats': [dict(c) for c in chats]})

@app.route('/api/chat/<int:cid>')
def api_chat(cid):
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    conn = get_db()
    is_member = conn.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?', (cid, user['id'])).fetchone()
    if not is_member: return jsonify({'error': 'access'}), 403
    chat = conn.execute('SELECT * FROM chats WHERE id=?', (cid,)).fetchone()
    members = conn.execute('''
        SELECT u.id, u.username, u.last_seen, cm.role FROM users u JOIN chat_members cm ON u.id=cm.user_id WHERE cm.chat_id=? ORDER BY cm.role DESC, u.username
    ''', (cid,)).fetchall()
    msgs = conn.execute('''
        SELECT m.*, u.username FROM messages m JOIN users u ON m.user_id=u.id WHERE m.chat_id=? ORDER BY m.id DESC LIMIT 100
    ''', (cid,)).fetchall()
    conn.close()
    return jsonify({'chat': dict(chat), 'members': [dict(m) for m in members], 'messages': [dict(m) for m in reversed(msgs)]})

@app.route('/api/create_chat', methods=['POST'])
def api_create_chat():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    data = request.json
    ctype = data.get('type', 'private')
    name = data.get('name', '').strip()
    desc = data.get('desc', '').strip()
    target_uid = data.get('target_uid')
    conn = get_db()
    if ctype == 'private' and target_uid:
        existing = conn.execute('''
            SELECT c.id FROM chats c
            JOIN chat_members m1 ON c.id=m1.chat_id AND m1.user_id=?
            JOIN chat_members m2 ON c.id=m2.chat_id AND m2.user_id=?
            WHERE c.type='private' GROUP BY c.id HAVING COUNT(*)=2
        ''', (user['id'], target_uid)).fetchone()
        if existing:
            conn.close()
            return jsonify({'ok': True, 'id': existing['id']})
        cur = conn.execute('INSERT INTO chats (type, name, description, created_by) VALUES (?,?,?,?)', ('private', None, None, user['id']))
        cid = cur.lastrowid
        conn.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (?,?,?)', (cid, user['id'], 'owner'))
        conn.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (?,?,?)', (cid, target_uid, 'member'))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': cid})
    else:
        if not name: return jsonify({'error': 'name'}), 400
        cur = conn.execute('INSERT INTO chats (type, name, description, created_by) VALUES (?,?,?,?)', ('channel', name, desc, user['id']))
        cid = cur.lastrowid
        conn.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (?,?,?)', (cid, user['id'], 'owner'))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': cid})

@app.route('/api/invite', methods=['POST'])
def api_invite():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    cid = request.json.get('chat_id')
    target_uid = request.json.get('target_uid')
    if not cid or not target_uid: return jsonify({'error': 'params'}), 400
    conn = get_db()
    try:
        conn.execute('INSERT INTO chat_members (chat_id, user_id) VALUES (?,?)', (cid, target_uid))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except:
        conn.close()
        return jsonify({'error': 'exists'}), 400

@socketio.on('join')
def on_join(data):
    cid = data.get('cid')
    if cid: join_room(f'chat_{cid}')

@socketio.on('msg')
def on_msg(data):
    user = get_user()
    if not user: return
    cid, content = data.get('cid'), data.get('content', '').strip()
    if not cid or not content: return
    conn = get_db()
    conn.execute('INSERT INTO messages (chat_id, user_id, content, created_at) VALUES (?,?,?,CURRENT_TIMESTAMP)', (cid, user['id'], content))
    conn.commit()
    conn.close()
    emit('msg', {'cid': cid, 'uid': user['id'], 'user': user['username'], 'text': content, 'time': datetime.now().strftime('%H:%M')}, room=f'chat_{cid}', broadcast=True)

if __name__ == '__main__':
    init_db()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)    user = get_user()
    if not user: return redirect('/login')
    update_seen(user['id'])
    conn = get_db()
    chats = conn.execute('''
        SELECT c.*, 
               (SELECT COUNT(*) FROM chat_members WHERE chat_id=c.id) as members_count,
               (SELECT content FROM messages WHERE chat_id=c.id ORDER BY id DESC LIMIT 1) as last_msg,
               (SELECT created_at FROM messages WHERE chat_id=c.id ORDER BY id DESC LIMIT 1) as last_msg_time
        FROM chats c
        JOIN chat_members cm ON c.id=cm.chat_id
        WHERE cm.user_id=?
        GROUP BY c.id
        ORDER BY c.id DESC
    ''', (user['id'],)).fetchall()
    conn.close()
    return render_template('index.html', user=user, chats=chats)

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u, p = request.form['username'].strip(), request.form['password']
        conn = get_db()
        row = conn.execute('SELECT * FROM users WHERE username=? AND password=?', (u, hash_pwd(p))).fetchone()
        conn.close()
        if row:
            session['uid'] = row['id']
            update_seen(row['id'])
            return redirect('/')
        return render_template('index.html', error='Неверный логин или пароль', login=True, user=None, chats=[])
    return render_template('index.html', login=True, user=None, chats=[])

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        u, p = request.form['username'].strip(), request.form['password']
        try:
            conn = get_db()
            conn.execute('INSERT INTO users (username, password) VALUES (?,?)', (u, hash_pwd(p)))
            conn.commit()
            row = conn.execute('SELECT * FROM users WHERE username=?', (u,)).fetchone()
            session['uid'] = row['id']
            conn.close()
            update_seen(row['id'])
            return redirect('/')
        except:
            return render_template('index.html', error='Имя уже занято', register=True, user=None, chats=[])
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
    conn = get_db()
    users = conn.execute('SELECT id, username, last_seen FROM users WHERE username LIKE ? AND id!=? LIMIT 8', (f'%{q}%', user['id'])).fetchall()
    chats = conn.execute('''
        SELECT c.id, c.name, c.type, c.description
        FROM chats c JOIN chat_members cm ON c.id=cm.chat_id
        WHERE cm.user_id=? AND c.name LIKE ? LIMIT 5
    ''', (user['id'], f'%{q}%')).fetchall()
    conn.close()
    return jsonify({'users': [dict(u) for u in users], 'chats': [dict(c) for c in chats]})

@app.route('/api/chat/<int:cid>')
def api_chat(cid):
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    conn = get_db()
    is_member = conn.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?', (cid, user['id'])).fetchone()
    if not is_member: return jsonify({'error': 'access'}), 403
    
    chat = conn.execute('SELECT * FROM chats WHERE id=?', (cid,)).fetchone()
    members = conn.execute('''
        SELECT u.id, u.username, u.last_seen, cm.role 
        FROM users u JOIN chat_members cm ON u.id=cm.user_id 
        WHERE cm.chat_id=? ORDER BY cm.role DESC, u.username
    ''', (cid,)).fetchall()
    msgs = conn.execute('''
        SELECT m.*, u.username 
        FROM messages m JOIN users u ON m.user_id=u.id 
        WHERE m.chat_id=? ORDER BY m.id DESC LIMIT 100
    ''', (cid,)).fetchall()
    conn.close()
    return jsonify({
        'chat': dict(chat),
        'members': [dict(m) for m in members],
        'messages': [dict(m) for m in reversed(msgs)]
    })

@app.route('/api/create_chat', methods=['POST'])
def api_create_chat():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    data = request.json
    ctype = data.get('type', 'private')
    name = data.get('name', '').strip()
    desc = data.get('desc', '').strip()
    target_uid = data.get('target_uid')

    conn = get_db()
    if ctype == 'private' and target_uid:
        existing = conn.execute('''
            SELECT c.id FROM chats c
            JOIN chat_members m1 ON c.id=m1.chat_id AND m1.user_id=?
            JOIN chat_members m2 ON c.id=m2.chat_id AND m2.user_id=?
            WHERE c.type='private' GROUP BY c.id HAVING COUNT(*)=2
        ''', (user['id'], target_uid)).fetchone()
        if existing:
            conn.close()
            return jsonify({'ok': True, 'id': existing['id']})
        
        cur = conn.execute('INSERT INTO chats (type, name, description, created_by) VALUES (?,?,?,?)', 
                           ('private', None, None, user['id']))
        cid = cur.lastrowid
        conn.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (?,?,?)', (cid, user['id'], 'owner'))
        conn.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (?,?,?)', (cid, target_uid, 'member'))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': cid})
    else:
        if not name: return jsonify({'error': 'name'}), 400
        cur = conn.execute('INSERT INTO chats (type, name, description, created_by) VALUES (?,?,?,?)', 
                           ('channel', name, desc, user['id']))
        cid = cur.lastrowid
        conn.execute('INSERT INTO chat_members (chat_id, user_id, role) VALUES (?,?,?)', (cid, user['id'], 'owner'))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'id': cid})

@app.route('/api/invite', methods=['POST'])
def api_invite():
    user = get_user()
    if not user: return jsonify({'error': 'auth'}), 401
    cid = request.json.get('chat_id')
    target_uid = request.json.get('target_uid')
    if not cid or not target_uid: return jsonify({'error': 'params'}), 400
    
    conn = get_db()
    try:
        conn.execute('INSERT INTO chat_members (chat_id, user_id) VALUES (?,?)', (cid, target_uid))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except:
        conn.close()
        return jsonify({'error': 'exists'}), 400

@socketio.on('join')
def on_join(data):
    cid = data.get('cid')
    if cid: join_room(f'chat_{cid}')

@socketio.on('msg')
def on_msg(data):
    user = get_user()
    if not user: return
    cid, content = data.get('cid'), data.get('content', '').strip()
    if not cid or not content: return
    conn = get_db()
    conn.execute('INSERT INTO messages (chat_id, user_id, content, created_at) VALUES (?,?,?,CURRENT_TIMESTAMP)', 
                 (cid, user['id'], content))
    conn.commit()
    conn.close()
    emit('msg', {
        'cid': cid, 'uid': user['id'], 'user': user['username'], 
        'text': content, 'time': datetime.now().strftime('%H:%M')
    }, room=f'chat_{cid}', broadcast=True)

if __name__ == '__main__':
    init_db()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
