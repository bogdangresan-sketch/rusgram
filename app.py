import os
import re
import random
import string
import json
import time
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Critical: gevent monkey-patch BEFORE any other imports
try:
    from gevent import monkey
    monkey.patch_all()
except ImportError:
    pass

from flask import Flask, render_template, request, redirect, session, url_for, jsonify, send_from_directory, Response, g, stream_with_context
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'rusgram-secret-key-dev')
app.config['DATABASE'] = os.environ.get('DATABASE_URL') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rusgram.db')
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

SECRET_CODE = '3257'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

class DB:
    def __init__(self, url):
        self.is_pg = url and 'postgres' in url.lower()
        if self.is_pg:
            import psycopg2
            import psycopg2.extras
            self.psycopg2 = psycopg2
            self.conn = psycopg2.connect(url)
            self.conn.autocommit = False
        else:
            sqlite3 = __import__('sqlite3')
            self.conn = sqlite3.connect(url, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA busy_timeout=30000')
            self.conn.execute('PRAGMA synchronous=NORMAL')
        self.cursor = None
        self._closed = False

    def execute(self, sql, params=None):
        if params is None: params = ()
        if self.is_pg:
            sql = self._pg(sql)
            if not sql: return self
            raw = self.conn.cursor(cursor_factory=self.psycopg2.extras.RealDictCursor)
            try:
                raw.execute(sql, params)
            except self.psycopg2.errors.UndefinedColumn:
                self.conn.rollback()
                sql2 = self._pg(sql, force_no_returning=True)
                raw2 = self.conn.cursor(cursor_factory=self.psycopg2.extras.RealDictCursor)
                raw2.execute(sql2, params)
                raw = raw2
            lid = None
            if sql.strip().upper().startswith('INSERT') and 'RETURNING' in sql.upper():
                try:
                    row = raw.fetchone()
                    lid = row['id'] if row else None
                except:
                    lid = None
            class CursorWrap:
                def __getattr__(s, n): return getattr(raw, n)
                @property
                def lastrowid(s): return lid
                @lastrowid.setter
                def lastrowid(s, v): pass
            self.cursor = CursorWrap()
        else:
            self.cursor = self.conn.execute(sql, params)
        return self.cursor

    def _pg(self, sql, force_no_returning=False):
        if force_no_returning:
            sql = sql.replace(' RETURNING id', '')
        s = sql.strip().upper()
        if s.startswith('PRAGMA'):
            return ''
        sql = sql.replace('?', '%s')
        sql = sql.replace("datetime('now')", "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')")
        sql = sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY')
        no_id_tables = ['chat_members', 'blocked_users']
        m = re.search(r"INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(\w+)", sql, re.I)
        has_no_id = m and m.group(1).lower() in no_id_tables
        add_returning = not force_no_returning and not has_no_id and s.startswith('INSERT') and 'RETURNING' not in s
        if 'INSERT OR IGNORE' in sql:
            sql = sql.replace('INSERT OR IGNORE', 'INSERT') + ' ON CONFLICT DO NOTHING'
        if add_returning:
            sql = sql.rstrip(';') + ' RETURNING id'
        return sql

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        if self._closed: return
        self._closed = True
        try:
            if self.cursor:
                self.cursor.close()
                self.cursor = None
        except: pass
        try:
            self.conn.close()
        except: pass

def get_db():
    if 'db' not in g or g.db._closed:
        g.db = DB(app.config['DATABASE'])
        g.db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                avatar TEXT DEFAULT NULL,
                last_seen TEXT DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        g.db.execute("INSERT OR IGNORE INTO users (id, username, password) VALUES (999, 'Rusgram', '')")
        g.db.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                type TEXT NOT NULL DEFAULT 'private',
                created_by INTEGER DEFAULT NULL,
                pinned INTEGER DEFAULT 0,
                pinned_at TEXT DEFAULT NULL,
                accent_color TEXT DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        g.db.execute('''
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT DEFAULT 'member',
                draft TEXT DEFAULT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')
        g.db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT DEFAULT '',
                edited INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                file TEXT DEFAULT NULL,
                file_type TEXT DEFAULT NULL,
                reply_to INTEGER DEFAULT NULL,
                pinned INTEGER DEFAULT 0,
                status TEXT DEFAULT 'sent',
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        g.db.execute('''
            CREATE TABLE IF NOT EXISTS reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(message_id, user_id, emoji)
            )
        ''')
        g.db.execute('''
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, blocked_id)
            )
        ''')
        g.db.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        g.db.commit()
        # Migrations after commit so PostgreSQL doesn't abort the transaction
        for col in ['reply_to', 'pinned', 'status']:
            try:
                g.db.execute(f'ALTER TABLE messages ADD COLUMN {col} TEXT DEFAULT NULL' if col in ('status',) else f'ALTER TABLE messages ADD COLUMN {col} INTEGER DEFAULT NULL')
                g.db.commit()
            except:
                try: g.db.rollback()
                except: pass
        for col in ['pinned', 'accent_color']:
            try:
                g.db.execute(f'ALTER TABLE chats ADD COLUMN {col} TEXT DEFAULT NULL' if col == 'accent_color' else f'ALTER TABLE chats ADD COLUMN {col} INTEGER DEFAULT 0')
                g.db.commit()
            except:
                try: g.db.rollback()
                except: pass
        for col in ['role', 'draft']:
            try:
                g.db.execute(f'ALTER TABLE chat_members ADD COLUMN {col} TEXT DEFAULT NULL' if col == 'draft' else f'ALTER TABLE chat_members ADD COLUMN {col} TEXT DEFAULT \'member\'')
                g.db.commit()
            except:
                try: g.db.rollback()
                except: pass
        for col in ['anonymous_name']:
            try:
                g.db.execute(f'ALTER TABLE messages ADD COLUMN {col} TEXT DEFAULT NULL')
                g.db.commit()
            except:
                try: g.db.rollback()
                except: pass
    try: _dedup_private_chats(g.db)
    except: pass
    return g.db

def effective_user_id():
    uid = session.get('user_id')
    if uid == 1 and session.get('anonymous_mode'):
        return 999
    return uid

def close_db(e=None):
    db = g.pop('db', None)
    if db:
        try: db.close()
        except: pass

app.teardown_appcontext(close_db)

def _dedup_private_chats(db):
    chats = db.execute('''
        SELECT c.id FROM chats c WHERE c.type = 'private' ORDER BY c.id
    ''').fetchall()
    groups = {}
    for c in chats:
        mids = tuple(sorted(
            r['user_id'] for r in db.execute(
                'SELECT user_id FROM chat_members WHERE chat_id = ? AND user_id != 999', (c['id'],)
            ).fetchall()
        ))
        if len(mids) >= 2:
            groups.setdefault(mids, []).append(c['id'])
    for mids, ids in groups.items():
        if len(ids) <= 1:
            continue
        keep_id = ids[0]
        for dup_id in ids[1:]:
            db.execute('UPDATE messages SET chat_id = ? WHERE chat_id = ?', (keep_id, dup_id))
            db.execute('DELETE FROM chat_members WHERE chat_id = ?', (dup_id,))
            db.execute('DELETE FROM reports WHERE chat_id = ?', (dup_id,))
            db.execute('DELETE FROM chats WHERE id = ?', (dup_id,))
    if any(len(v) > 1 for v in groups.values()):
        db.commit()

def is_online(last_seen_str):
    if not last_seen_str:
        return False
    try:
        if 'T' not in last_seen_str:
            last = datetime.strptime(last_seen_str, '%Y-%m-%d %H:%M:%S')
        else:
            last = datetime.fromisoformat(last_seen_str.replace('Z', ''))
        return (datetime.now() - last).total_seconds() < 300
    except:
        return False

def last_seen_text(last_seen_str):
    if not last_seen_str:
        return 'недавно'
    try:
        if 'T' not in last_seen_str:
            last = datetime.strptime(last_seen_str, '%Y-%m-%d %H:%M:%S')
        else:
            last = datetime.fromisoformat(last_seen_str.replace('Z', ''))
        delta = datetime.now() - last
        if delta.total_seconds() < 60:
            return 'только что'
        if delta.total_seconds() < 3600:
            m = int(delta.total_seconds() // 60)
            return f'был(а) {m} мин. назад'
        if delta.total_seconds() < 86400:
            h = int(delta.total_seconds() // 3600)
            return f'был(а) {h} ч. назад'
        if delta.days < 7:
            return f'был(а) {delta.days} дн. назад'
        return last.strftime('%d.%m.%Y')
    except:
        return 'недавно'

# ===== Block / Unblock =====
@app.route('/api/block/<int:target_id>', methods=['POST'])
def api_block(target_id):
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); db = get_db()
    action = request.form.get('action', 'block')
    if action == 'block':
        db.execute('INSERT OR IGNORE INTO blocked_users VALUES (?, ?)', (uid, target_id))
    else:
        db.execute('DELETE FROM blocked_users WHERE user_id = ? AND blocked_id = ?', (uid, target_id))
    db.commit(); db.close()
    return jsonify({'ok': True})

# ===== Pin / Unpin Chat =====
@app.route('/api/pin_chat/<int:chat_id>', methods=['POST'])
def api_pin_chat(chat_id):
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    db = get_db(); uid = effective_user_id()
    chat = db.execute('SELECT pinned FROM chats WHERE id = ?', (chat_id,)).fetchone()
    if not chat: db.close(); return jsonify({'error': 'no chat'}), 404
    new_val = 0 if chat['pinned'] else 1
    db.execute('UPDATE chats SET pinned = ?, pinned_at = datetime("now") WHERE id = ?', (new_val, chat_id))
    db.commit(); db.close()
    return jsonify({'ok': True, 'pinned': new_val})

# ===== Clear Chat History =====
@app.route('/api/clear_chat/<int:chat_id>', methods=['POST'])
def api_clear_chat(chat_id):
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    db = get_db(); uid = effective_user_id()
    member = db.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?', (chat_id, uid)).fetchone()
    if not member: db.close(); return jsonify({'error': 'not member'}), 403
    db.execute('UPDATE messages SET deleted = 1 WHERE chat_id = ?', (chat_id,))
    db.commit(); db.close()
    return jsonify({'ok': True})

# ===== Pin / Unpin Message in Chat =====
@app.route('/api/pin_message/<int:msg_id>', methods=['POST'])
def api_pin_message(msg_id):
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    db = get_db(); uid = effective_user_id()
    msg = db.execute('SELECT m.id, m.chat_id, m.pinned FROM messages m JOIN chat_members cm ON m.chat_id = cm.chat_id AND cm.user_id = ? WHERE m.id = ?', (uid, msg_id)).fetchone()
    if not msg: db.close(); return jsonify({'error': 'not found'}), 404
    new_val = 0 if msg['pinned'] else 1
    if new_val: db.execute('UPDATE messages SET pinned = 1 WHERE chat_id = ? AND pinned = 1', (msg['chat_id'],))  # unpin old
    db.execute('UPDATE messages SET pinned = ? WHERE id = ?', (new_val, msg_id))
    db.commit(); db.close()
    return jsonify({'ok': True, 'pinned': new_val})

# ===== Delete For Everyone =====
@app.route('/api/delete_for_all', methods=['POST'])
def api_delete_for_all():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); msg_id = data['message_id']
    db = get_db()
    msg = db.execute('SELECT m.*, c.created_by FROM messages m JOIN chats c ON m.chat_id = c.id WHERE m.id = ?', (msg_id,)).fetchone()
    if not msg or (msg['user_id'] != uid and msg['created_by'] != uid):
        db.close(); return jsonify({'error': 'not allowed'}), 403
    db.execute('UPDATE messages SET deleted = 1, content = \'Сообщение удалено\' WHERE id = ?', (msg_id,))
    db.commit(); db.close()
    return jsonify({'ok': True})

# ===== Group Admin Actions =====
@app.route('/api/group_action/<int:chat_id>', methods=['POST'])
def api_group_action(chat_id):
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); db = get_db()
    chat = db.execute('SELECT * FROM chats WHERE id = ? AND type = ?', (chat_id, 'group')).fetchone()
    if not chat or chat['created_by'] != uid: db.close(); return jsonify({'error': 'not allowed'}), 403
    target_id = request.form.get('target_id', type=int); action = request.form.get('action')
    if target_id and action == 'make_admin':
        db.execute('UPDATE chat_members SET role = ? WHERE chat_id = ? AND user_id = ?', ('admin', chat_id, target_id))
    elif target_id and action == 'remove_admin':
        db.execute("UPDATE chat_members SET role = 'member' WHERE chat_id = ? AND user_id = ?", (chat_id, target_id))
    if target_id and action == 'remove':
        db.execute('DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?', (chat_id, target_id))
    db.commit(); db.close()
    return redirect(url_for('chat', chat_id=chat_id))

# ===== Report Message =====
@app.route('/api/report_message', methods=['POST'])
def api_report_message():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); msg_id = data['message_id']
    db = get_db()
    msg = db.execute('SELECT m.id, m.content, m.chat_id, m.user_id as author_id, u.username as author_name FROM messages m JOIN users u ON m.user_id = u.id WHERE m.id = ?', (msg_id,)).fetchone()
    if not msg: db.close(); return jsonify({'error': 'message not found'}), 404
    existing = db.execute('SELECT id FROM reports WHERE message_id = ? AND reporter_id = ?', (msg_id, uid)).fetchone()
    if not existing:
        db.execute('INSERT INTO reports (message_id, reporter_id, chat_id, author_id) VALUES (?, ?, ?, ?)',
                   (msg_id, uid, msg['chat_id'], msg['author_id']))
        db.commit()
    db.close()
    return jsonify({'ok': True, 'msg': 'Спасибо, жалоба отправлена администратору'})

@app.route('/admin/reports')
def admin_reports():
    if 'user_id' not in session: return redirect(url_for('login'))
    if session['user_id'] != 1: return redirect(url_for('chats'))
    db = get_db()
    reports = db.execute('''
        SELECT r.id, r.created_at, r.message_id, r.chat_id, r.author_id,
               m.content as msg_content, m.file, m.file_type,
               reporter.username as reporter_name, author.username as author_name
        FROM reports r
        JOIN messages m ON r.message_id = m.id
        JOIN users reporter ON r.reporter_id = reporter.id
        JOIN users author ON r.author_id = author.id
        ORDER BY r.id DESC
    ''').fetchall()
    db.close()
    return render_template('reports.html', reports=reports)

@app.route('/admin/delete_report/<int:rid>', methods=['POST'])
def admin_delete_report(rid):
    if 'user_id' not in session or session['user_id'] != 1: return jsonify({'error': 'not allowed'}), 403
    db = get_db()
    db.execute('DELETE FROM reports WHERE id = ?', (rid,))
    db.commit(); db.close()
    return redirect(url_for('admin_reports'))

@app.route('/admin')
def admin_panel():
    if 'user_id' not in session or session['user_id'] != 1: return redirect(url_for('chats'))
    return render_template('admin.html', creator_mode=session.get('creator_mode', False), anonymous_mode=session.get('anonymous_mode', False))

@app.route('/api/creator_mode', methods=['POST'])
def api_creator_mode():
    if 'user_id' not in session or session['user_id'] != 1: return jsonify({'error': 'not allowed'}), 403
    data = request.get_json()
    session['creator_mode'] = data.get('enabled', False)
    return jsonify({'ok': True, 'enabled': session['creator_mode']})

@app.route('/api/anonymous_mode', methods=['POST'])
def api_anonymous_mode():
    if 'user_id' not in session or session['user_id'] != 1: return jsonify({'error': 'not allowed'}), 403
    data = request.get_json()
    session['anonymous_mode'] = data.get('enabled', False)
    return jsonify({'ok': True, 'enabled': session['anonymous_mode']})

# ===== Save Draft =====
@app.route('/api/draft', methods=['POST'])
def api_draft():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); chat_id = data['chat_id']; text = data.get('text', '')
    db = get_db()
    db.execute('UPDATE chat_members SET draft = ? WHERE chat_id = ? AND user_id = ?', (text, chat_id, uid))
    db.commit(); db.close()
    return jsonify({'ok': True})

# ===== Typing Indicator =====
typing_tracker = {}
# Call tracker: {call_id: {chat_id, caller_id, callee_id, type, state, sdp_offer, sdp_answer, ice_candidates: [], signal_events: []}}
call_tracker = {}
call_counter = [0]

# ===== Call API =====
@app.route('/api/start_call', methods=['POST'])
def api_start_call():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); chat_id = data['chat_id']; call_type = data.get('type', 'audio')
    db = get_db()
    members = db.execute('SELECT user_id FROM chat_members WHERE chat_id = ? AND user_id != ?', (chat_id, uid)).fetchall()
    db.close()
    if not members: return jsonify({'error': 'no other member'}), 400
    callee_id = members[0]['user_id']
    call_counter[0] += 1
    call_id = call_counter[0]
    call_tracker[call_id] = {'chat_id': chat_id, 'caller_id': uid, 'callee_id': callee_id, 'type': call_type, 'state': 'ringing', 'signal_events': []}
    # Add ringing event for callee
    call_tracker[call_id]['signal_events'].append({'event': 'incoming_call', 'call_id': call_id, 'caller_id': uid, 'type': call_type})
    return jsonify({'ok': True, 'call_id': call_id})

@app.route('/api/answer_call', methods=['POST'])
def api_answer_call():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); call_id = data['call_id']; accept = data.get('accept', True)
    call = call_tracker.get(call_id)
    if not call: return jsonify({'error': 'call not found'}), 404
    if call['callee_id'] != uid: return jsonify({'error': 'not your call'}), 403
    if accept:
        call['state'] = 'connecting'
        call['signal_events'].append({'event': 'call_accepted', 'call_id': call_id})
    else:
        call['state'] = 'ended'
        call['signal_events'].append({'event': 'call_rejected', 'call_id': call_id})
    return jsonify({'ok': True})

@app.route('/api/end_call', methods=['POST'])
def api_end_call():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); call_id = data['call_id']
    call = call_tracker.get(call_id)
    if call:
        call['state'] = 'ended'
        call['signal_events'].append({'event': 'call_ended', 'call_id': call_id})
    return jsonify({'ok': True})

@app.route('/api/call_signal', methods=['POST'])
def api_call_signal():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); call_id = data['call_id']
    call = call_tracker.get(call_id)
    if not call: return jsonify({'error': 'call not found'}), 404
    call['signal_events'].append({'event': 'signal', 'call_id': call_id, 'from': uid, 'type': data.get('signal_type'), 'data': data.get('data')})
    return jsonify({'ok': True})
@app.route('/api/typing', methods=['POST'])
def api_typing():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); chat_id = data['chat_id']
    typing_tracker[f'{chat_id}:{uid}'] = time.time()
    return jsonify({'ok': True})

# ===== Chat Accent Color =====
@app.route('/api/set_accent/<int:chat_id>', methods=['POST'])
def api_set_accent(chat_id):
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    db = get_db(); uid = effective_user_id()
    member = db.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?', (chat_id, uid)).fetchone()
    if not member: db.close(); return jsonify({'error': 'not member'}), 403
    color = request.form.get('color')
    db.execute('UPDATE chats SET accent_color = ? WHERE id = ?', (color, chat_id))
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/api/delete_for_me', methods=['POST'])
def api_delete_for_me():
    if 'user_id' not in session: return jsonify({'error': 'not logged in'}), 401
    uid = effective_user_id(); data = request.get_json(); msg_id = data['message_id']
    db = get_db()
    db.execute('UPDATE messages SET deleted = 1 WHERE id = ? AND user_id = ?', (msg_id, uid))
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.before_request
def update_last_seen():
    if 'user_id' in session:
        now = datetime.now()
        last_str = session.get('last_seen_update')
        if not last_str or (now - datetime.fromisoformat(last_str)).total_seconds() > 30:
            db = get_db()
            db.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now.strftime('%Y-%m-%d %H:%M:%S'), effective_user_id()))
            db.commit()
            session['last_seen_update'] = now.isoformat()

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('chats'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('chats'))
        return render_template('login.html', error='Неверное имя или пароль')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        code = request.form['code']
        if code != SECRET_CODE:
            return render_template('register.html', error='Неверный секретный код')
        db = get_db()
        existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            db.close()
            return render_template('register.html', error='Имя уже занято')
        hashed = generate_password_hash(password)
        db.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, hashed))
        db.commit()
        db.close()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/chats')
def chats():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    user_id = effective_user_id()
    chat_list = db.execute('''
        SELECT c.id, c.name, c.type, c.created_by, c.pinned, c.pinned_at, c.accent_color,
            cm.draft,
            (SELECT content FROM messages WHERE chat_id = c.id AND deleted = 0 ORDER BY id DESC LIMIT 1) as last_msg,
            (SELECT file_type FROM messages WHERE chat_id = c.id AND deleted = 0 ORDER BY id DESC LIMIT 1) as last_msg_file_type,
            (SELECT user_id FROM messages WHERE chat_id = c.id AND deleted = 0 ORDER BY id DESC LIMIT 1) as last_msg_user_id,
            (SELECT created_at FROM messages WHERE chat_id = c.id AND deleted = 0 ORDER BY id DESC LIMIT 1) as last_msg_at
        FROM chats c
        JOIN chat_members cm ON c.id = cm.chat_id
        WHERE cm.user_id = ? AND c.type != 'saved'
    ''', (user_id,)).fetchall()
    real_id = session.get('user_id')
    users = db.execute('SELECT id, username, avatar, last_seen FROM users WHERE id != ? AND id != 999', (real_id,)).fetchall()
    current_user = db.execute('SELECT id, avatar, username FROM users WHERE id = ?', (user_id,)).fetchone()
    chat_list = [dict(c) for c in chat_list]
    for c in chat_list:
        if c['type'] == 'private':
            other = db.execute('''
                SELECT u.username, u.last_seen FROM users u
                JOIN chat_members cm ON u.id = cm.user_id
                WHERE cm.chat_id = ? AND cm.user_id != ? AND cm.user_id != 999
            ''', (c['id'], user_id)).fetchone()
            c['display_name'] = other['username'] if other else 'Чат'
            c['other_online'] = is_online(other['last_seen']) if other else False
            c['other_last_seen'] = last_seen_text(other['last_seen']) if other else ''
        else:
            c['display_name'] = c['name']
        c['pinned'] = bool(c['pinned'])
    # Pinned first, then by last message (most recent first)
    chat_list.sort(key=lambda x: x.get('last_msg_at') or '', reverse=True)
    chat_list.sort(key=lambda x: 0 if x['pinned'] else 1)
    users = [dict(u) for u in users]
    for u in users:
        u['online'] = is_online(u['last_seen'])
    db.close()
    return render_template('chats.html', chats=chat_list, users=users, current_user=current_user, creator_mode=session.get('creator_mode', False), anonymous_mode=session.get('anonymous_mode', False))

@app.route('/chat/<int:chat_id>')
def chat(chat_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    eff_id = effective_user_id()
    real_id = session.get('user_id')
    member = db.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?',
                        (chat_id, eff_id)).fetchone()
    if not member:
        if eff_id != real_id:
            real_member = db.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?',
                                     (chat_id, real_id)).fetchone()
            if real_member:
                db.execute('INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, eff_id))
                db.commit()
                member = True
            else:
                db.close()
                return redirect(url_for('chats'))
        else:
            db.close()
            return redirect(url_for('chats'))
    chat_info = dict(db.execute('SELECT * FROM chats WHERE id = ?', (chat_id,)).fetchone())
    pinned_msgs = [dict(r) for r in db.execute('''
        SELECT m.id, m.content, m.created_at, m.file, m.file_type, m.reply_to, m.user_id, u.username
        FROM messages m JOIN users u ON m.user_id = u.id
        WHERE m.chat_id = ? AND m.deleted = 0 AND m.pinned = 1
        ORDER BY m.id DESC
    ''', (chat_id,)).fetchall()]
    rows = db.execute('''
        SELECT m.id, m.content, m.created_at, m.edited, m.deleted, m.file, m.file_type, m.reply_to, m.pinned, m.status, m.user_id,
               u.username, u.avatar as author_avatar
        FROM messages m
        JOIN users u ON m.user_id = u.id
        WHERE m.chat_id = ? AND m.deleted = 0
        ORDER BY m.id ASC
    ''', (chat_id,)).fetchall()
    messages = attach_reactions(rows, eff_id, db)
    reply_ids = [m.get('reply_to') for m in messages if m.get('reply_to')]
    if reply_ids:
        ph = ','.join('?' * len(reply_ids))
        replies = {r['id']: r for r in db.execute(f'''
            SELECT m.id, m.content, m.file, m.file_type, u.username
            FROM messages m JOIN users u ON m.user_id = u.id
            WHERE m.id IN ({ph}) AND m.deleted = 0
        ''', reply_ids).fetchall()}
        for m in messages:
            if m.get('reply_to') and m['reply_to'] in replies:
                m['reply'] = replies[m['reply_to']]
    members = db.execute('''
        SELECT u.id, u.username, u.avatar, cm.role
        FROM users u
        JOIN chat_members cm ON u.id = cm.user_id
        WHERE cm.chat_id = ? AND u.id != 999
    ''', (chat_id,)).fetchall()
    draft = db.execute('SELECT draft FROM chat_members WHERE chat_id = ? AND user_id = ?', (chat_id, eff_id)).fetchone()
    real_id = session.get('user_id')
    all_users = db.execute('SELECT id, username, avatar FROM users WHERE id != ? AND id != 999', (real_id,)).fetchall()
    member_ids = {m['id'] for m in members}
    non_member_users = [u for u in all_users if u['id'] not in member_ids]
    current_user = db.execute('SELECT id, username, avatar FROM users WHERE id = ?', (eff_id,)).fetchone()
    if chat_info['type'] == 'private':
        other = db.execute('''
            SELECT u.id, u.username, u.last_seen FROM users u
            JOIN chat_members cm ON u.id = cm.user_id
            WHERE cm.chat_id = ? AND cm.user_id != ? AND cm.user_id != 999
        ''', (chat_id, eff_id)).fetchone()
        other_user_id = other['id'] if other else None
        display_name = other['username'] if other else 'Чат'
        other_online = is_online(other['last_seen']) if other else False
        other_last_seen_text = last_seen_text(other['last_seen']) if other else ''
        is_blocked = bool(db.execute('SELECT 1 FROM blocked_users WHERE user_id = ? AND blocked_id = ?', (eff_id, other['id'])).fetchone()) if other else False
    else:
        display_name = chat_info['name']
        other_online = False
        other_last_seen_text = ''
        is_blocked = False
        other_user_id = None
    from datetime import timedelta
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    db.close()
    return render_template('chat.html', chat=chat_info, messages=messages, pinned_messages=pinned_msgs, members=members, draft=draft['draft'] if draft else '', all_users=all_users, non_member_users=non_member_users, current_user=current_user, display_name=display_name, other_online=other_online, other_last_seen_text=other_last_seen_text, is_blocked=is_blocked, other_user_id=other_user_id, today=today, yesterday=yesterday, creator_mode=session.get('creator_mode', False), anonymous_mode=session.get('anonymous_mode', False))

@app.route('/start_private/<int:other_id>')
def start_private(other_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    eff_id = effective_user_id()
    real_id = session.get('user_id')

    existing = db.execute('''
        SELECT c.id FROM chats c
        WHERE c.type = 'private'
        AND EXISTS (SELECT 1 FROM chat_members WHERE chat_id = c.id AND user_id = ?)
        AND EXISTS (SELECT 1 FROM chat_members WHERE chat_id = c.id AND user_id = ?)
    ''', (eff_id, other_id)).fetchone()

    if not existing and eff_id != real_id:
        existing = db.execute('''
            SELECT c.id FROM chats c
            WHERE c.type = 'private'
            AND EXISTS (SELECT 1 FROM chat_members WHERE chat_id = c.id AND user_id = ?)
            AND EXISTS (SELECT 1 FROM chat_members WHERE chat_id = c.id AND user_id = ?)
        ''', (real_id, other_id)).fetchone()

    if existing:
        chat_id = existing['id']
        if eff_id != real_id:
            db.execute('INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, eff_id))
    else:
        cursor = db.execute("INSERT INTO chats (type, created_by) VALUES ('private', ?)", (eff_id,))
        chat_id = cursor.lastrowid
        db.execute('INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, eff_id))
        db.execute('INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, other_id))
        if eff_id != real_id:
            db.execute('INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, real_id))
    db.commit()
    db.close()
    return redirect(url_for('chat', chat_id=chat_id))

@app.route('/create_group', methods=['POST'])
def create_group():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    name = request.form['name']
    member_ids = request.form.getlist('members')
    db = get_db()
    user_id = effective_user_id()
    cursor = db.execute("INSERT INTO chats (name, type, created_by) VALUES (?, 'group', ?)", (name, user_id))
    chat_id = cursor.lastrowid
    db.execute('INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, user_id))
    for mid in member_ids:
        db.execute('INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, int(mid)))
    db.commit()
    db.close()
    return redirect(url_for('chat', chat_id=chat_id))

@app.route('/delete_group/<int:chat_id>', methods=['POST'])
def delete_group(chat_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    user_id = effective_user_id()
    chat = db.execute('SELECT * FROM chats WHERE id = ? AND type = ?', (chat_id, 'group')).fetchone()
    if not chat or chat['created_by'] != user_id:
        db.close()
        return redirect(url_for('chats'))
    db.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
    db.execute('DELETE FROM chat_members WHERE chat_id = ?', (chat_id,))
    db.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
    db.commit()
    db.close()
    return redirect(url_for('chats'))

@app.route('/add_member/<int:chat_id>', methods=['POST'])
def add_member(chat_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    user_id = effective_user_id()
    chat = db.execute('SELECT * FROM chats WHERE id = ? AND type = ?', (chat_id, 'group')).fetchone()
    if not chat or chat['created_by'] != user_id:
        db.close()
        return redirect(url_for('chat', chat_id=chat_id))
    target_id = request.form.get('user_id', type=int)
    if target_id and not db.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?', (chat_id, target_id)).fetchone():
        db.execute('INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, target_id))
        db.commit()
    db.close()
    return redirect(url_for('chat', chat_id=chat_id))

@app.route('/remove_member/<int:chat_id>/<int:target_id>', methods=['POST'])
def remove_member(chat_id, target_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    user_id = effective_user_id()
    chat = db.execute('SELECT * FROM chats WHERE id = ? AND type = ?', (chat_id, 'group')).fetchone()
    if not chat or chat['created_by'] != user_id:
        db.close()
        return redirect(url_for('chat', chat_id=chat_id))
    db.execute('DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?', (chat_id, target_id))
    db.commit()
    db.close()
    return redirect(url_for('chat', chat_id=chat_id))

@app.route('/upload_avatar', methods=['POST'])
def upload_avatar():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if 'avatar' not in request.files:
        return redirect(url_for('chats'))
    file = request.files['avatar']
    if file.filename == '':
        return redirect(url_for('chats'))
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'jpg'
    filename = f'avatar_{effective_user_id()}.{ext}'
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    db = get_db()
    db.execute('UPDATE users SET avatar = ? WHERE id = ?', (filename, effective_user_id()))
    db.commit()
    db.close()
    session['avatar'] = filename
    return redirect(request.referrer or url_for('chats'))

@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/messages/<int:chat_id>')
def api_messages(chat_id):
    if 'user_id' not in session:
        return jsonify([])
    db = get_db()
    since = request.args.get('since', 0, type=int)
    msgs = db.execute('''
        SELECT m.id, m.content, m.created_at, m.edited, m.file, m.file_type, m.reply_to, m.status, u.username, m.user_id
        FROM messages m
        JOIN users u ON m.user_id = u.id
        WHERE m.chat_id = ? AND m.id > ? AND m.deleted = 0
        ORDER BY m.id ASC
    ''', (chat_id, since)).fetchall()
    msgs = attach_reactions(msgs, effective_user_id(), db)
    # Attach reply info
    reply_ids = [m['reply_to'] for m in msgs if m.get('reply_to')]
    if reply_ids:
        ph = ','.join('?' * len(reply_ids))
        replies = {r['id']: r for r in db.execute(f'''
            SELECT m.id, m.content, m.file, m.file_type, u.username
            FROM messages m JOIN users u ON m.user_id = u.id
            WHERE m.id IN ({ph}) AND m.deleted = 0
        ''', reply_ids).fetchall()}
        for m in msgs:
            if m.get('reply_to') and m['reply_to'] in replies:
                m['reply'] = replies[m['reply_to']]
    db.close()
    return jsonify(msgs)

@app.route('/api/reactions/<int:chat_id>')
def api_reactions(chat_id):
    if 'user_id' not in session:
        return jsonify([])
    db = get_db()
    since = request.args.get('since', 0, type=int)
    rows = db.execute('''
        SELECT r.id, r.message_id, r.emoji, r.user_id
        FROM reactions r
        JOIN messages m ON r.message_id = m.id
        WHERE m.chat_id = ? AND m.deleted = 0 AND r.id > ?
        ORDER BY r.id ASC
    ''', (chat_id, since)).fetchall()
    data = []
    mid_to_reactions = {}
    for r in rows:
        mid = r['message_id']
        if mid not in mid_to_reactions:
            mid_to_reactions[mid] = []
        mid_to_reactions[mid].append({'emoji': r['emoji'], 'user_id': r['user_id']})
    for mid, reacs in mid_to_reactions.items():
        data.append({'message_id': mid, 'all_reactions': reacs})
    db.close()
    return jsonify(data)

ALLOWED_EXT = {'jpg','jpeg','png','gif','mp4','webm','mov','avi','opus','ogg','mp3','wav'}
def random_filename(ext):
    return f'msg_{datetime.now().strftime("%Y%m%d_%H%M%S")}_{"".join(random.choices(string.digits, k=4))}.{ext}'

@app.route('/api/send', methods=['POST'])
def api_send():
    if 'user_id' not in session:
        return jsonify({'error': 'not logged in'}), 401
    user_id = effective_user_id()

    if request.content_type and 'multipart/form-data' in request.content_type:
        chat_id = int(request.form['chat_id'])
        content = request.form.get('content', '').strip()
        file = request.files.get('file')
        reply_to = request.form.get('reply_to', type=int)
    else:
        data = request.get_json()
        chat_id = data['chat_id']
        content = data.get('content', '').strip()
        file = None
        reply_to = data.get('reply_to')

    if not content and not file:
        return jsonify({'error': 'empty'}), 400

    db = get_db()
    rusgram_id = 999
    is_anon = session.get('anonymous_mode') and session.get('user_id') == 1
    if is_anon:
        user_id = rusgram_id
        db.execute('INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, rusgram_id))
    member = db.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?',
                        (chat_id, user_id)).fetchone()
    if not member:
        db.close()
        return jsonify({'error': 'not a member'}), 403

    file_name = None
    file_type = None
    if file and file.filename:
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if ext in ALLOWED_EXT:
            if ext in ('jpg','jpeg','png'):
                file_type = 'image'
            elif ext == 'gif':
                file_type = 'gif'
            elif ext in ('ogg', 'wav', 'mp3', 'opus'):
                file_type = 'audio'
            else:
                file_type = 'video'
            file_name = random_filename(ext)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], file_name))

    cursor = db.execute('INSERT INTO messages (chat_id, user_id, content, file, file_type, reply_to) VALUES (?, ?, ?, ?, ?, ?)',
                        (chat_id, user_id, content, file_name, file_type, reply_to))
    db.commit()
    msg = db.execute('''
        SELECT m.id, m.content, m.created_at, m.edited, m.file, m.file_type, m.reply_to, u.username, m.user_id
        FROM messages m
        JOIN users u ON m.user_id = u.id
        WHERE m.id = ?
    ''', (cursor.lastrowid,)).fetchone()
    msg = attach_reactions([msg], user_id, db)[0]
    if msg.get('reply_to'):
        msg['reply'] = db.execute('''
            SELECT m.id, m.content, m.file, m.file_type, u.username
            FROM messages m JOIN users u ON m.user_id = u.id
            WHERE m.id = ? AND m.deleted = 0
        ''', (msg['reply_to'],)).fetchone()
    db.close()
    return jsonify(msg)

@app.route('/api/delete_message', methods=['POST'])
def api_delete_message():
    if 'user_id' not in session:
        return jsonify({'error': 'not logged in'}), 401
    data = request.get_json()
    msg_id = data['message_id']
    db = get_db()
    user_id = effective_user_id()
    msg = db.execute('SELECT * FROM messages WHERE id = ? AND user_id = ?', (msg_id, user_id)).fetchone()
    if not msg:
        db.close()
        return jsonify({'error': 'not found'}), 404
    db.execute('UPDATE messages SET deleted = 1 WHERE id = ?', (msg_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})

@app.route('/api/edit_message', methods=['POST'])
def api_edit_message():
    if 'user_id' not in session:
        return jsonify({'error': 'not logged in'}), 401
    data = request.get_json()
    msg_id = data['message_id']
    new_content = data['content'].strip()
    if not new_content:
        return jsonify({'error': 'empty'}), 400
    db = get_db()
    user_id = effective_user_id()
    msg = db.execute('SELECT * FROM messages WHERE id = ? AND user_id = ?', (msg_id, user_id)).fetchone()
    if not msg:
        db.close()
        return jsonify({'error': 'not found'}), 404
    db.execute('UPDATE messages SET content = ?, edited = 1 WHERE id = ?', (new_content, msg_id))
    db.commit()
    db.close()
    return jsonify({'ok': True})

@app.route('/api/forward', methods=['POST'])
def api_forward():
    if 'user_id' not in session:
        return jsonify({'error': 'not logged in'}), 401
    user_id = effective_user_id()
    data = request.get_json()
    msg_id = data['message_id']
    target_chat_ids = data['chat_ids']  # list
    db = get_db()
    original = db.execute('''
        SELECT m.content, m.file, m.file_type, u.username
        FROM messages m JOIN users u ON m.user_id = u.id
        WHERE m.id = ? AND m.deleted = 0
    ''', (msg_id,)).fetchone()
    if not original:
        db.close()
        return jsonify({'error': 'message not found'}), 404
    prefix = f"📨 Переслано от @{original['username']}\n"
    new_ids = []
    for cid in target_chat_ids:
        member = db.execute('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?', (cid, user_id)).fetchone()
        if not member:
            continue
        cursor = db.execute(
            'INSERT INTO messages (chat_id, user_id, content, file, file_type) VALUES (?, ?, ?, ?, ?)',
            (cid, user_id, prefix + (original['content'] or ''), original['file'], original['file_type'])
        )
        new_ids.append(cursor.lastrowid)
    db.commit()
    db.close()
    return jsonify({'ok': True, 'ids': new_ids})

def attach_reactions(msgs, user_id, db):
    if not msgs:
        return msgs
    msgs = [dict(m) for m in msgs]
    ids = [m['id'] for m in msgs]
    ph = ','.join('?' * len(ids))
    rows = db.execute(f'SELECT message_id, emoji, user_id FROM reactions WHERE message_id IN ({ph})', ids).fetchall()
    by_msg = {}
    for r in rows:
        mid = r['message_id']
        if mid not in by_msg:
            by_msg[mid] = []
        by_msg[mid].append({'emoji': r['emoji'], 'user_id': r['user_id']})
    for m in msgs:
        m['reactions'] = by_msg.get(m['id'], [])
    return msgs

@app.route('/api/react', methods=['POST'])
def api_react():
    if 'user_id' not in session:
        return jsonify({'error': 'not logged in'}), 401
    data = request.get_json()
    message_id = data['message_id']
    emoji = data['emoji']
    user_id = effective_user_id()
    db = get_db()
    existing = db.execute('SELECT id FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?',
                          (message_id, user_id, emoji)).fetchone()
    if existing:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing['id'],))
        action = 'removed'
    else:
        db.execute('INSERT OR IGNORE INTO reactions (message_id, user_id, emoji) VALUES (?, ?, ?)',
                   (message_id, user_id, emoji))
        action = 'added'
    db.commit()
    db.close()
    return jsonify({'action': action, 'message_id': message_id, 'emoji': emoji, 'user_id': user_id})

@app.route('/api/stream/<int:chat_id>')
def api_stream(chat_id):
    if 'user_id' not in session:
        return jsonify({'error': 'not logged in'}), 401
    def generate():
        last_id = request.args.get('since', 0, type=int)
        last_reaction = 0
        eff_id = effective_user_id()
        db = None
        try:
            db = get_db()
            while True:
                try:
                    try:
                        db.execute('SELECT 1')
                    except Exception:
                        try: db.close()
                        except: pass
                        db = get_db()

                    messages = db.execute('''
                        SELECT m.id, m.content, m.created_at, m.edited, m.deleted, m.file, m.file_type, m.reply_to, m.status, u.username, m.user_id
                        FROM messages m
                        JOIN users u ON m.user_id = u.id
                        WHERE m.chat_id = ? AND m.id > ? AND m.deleted = 0
                        ORDER BY m.id ASC
                    ''', (chat_id, last_id)).fetchall()
                    if messages:
                        messages = attach_reactions(messages, eff_id, db)
                        reply_ids = [m['reply_to'] for m in messages if m.get('reply_to')]
                        if reply_ids:
                            ph = ','.join('?' * len(reply_ids))
                            replies = {r['id']: r for r in db.execute(f'''
                                SELECT m.id, m.content, m.file, m.file_type, u.username
                                FROM messages m JOIN users u ON m.user_id = u.id
                                WHERE m.id IN ({ph}) AND m.deleted = 0
                            ''', reply_ids).fetchall()}
                            for m in messages:
                                if m.get('reply_to') and m['reply_to'] in replies:
                                    m['reply'] = replies[m['reply_to']]
                        last_id = messages[-1]['id']
                        for msg in messages:
                            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    reactions = db.execute('''
                        SELECT r.id, r.message_id, r.emoji, r.user_id
                        FROM reactions r
                        JOIN messages m ON r.message_id = m.id
                        WHERE m.chat_id = ? AND m.deleted = 0 AND r.id > ?
                        ORDER BY r.id ASC
                    ''', (chat_id, last_reaction)).fetchall()
                    if reactions:
                        last_reaction = reactions[-1]['id']
                        for r in reactions:
                            yield f"event: reaction\ndata: {json.dumps(dict(r), ensure_ascii=False)}\n\n"
                    # Typing indicator
                    now = time.time()
                    for key, t in list(typing_tracker.items()):
                        if now - t > 3:
                            del typing_tracker[key]
                        else:
                            parts = key.split(':')
                            if parts[0] == str(chat_id) and parts[1] != str(eff_id):
                                user = db.execute('SELECT username FROM users WHERE id = ?', (parts[1],)).fetchone()
                                if user:
                                    yield f"event: typing\ndata: {json.dumps({'user_id': int(parts[1]), 'username': user['username']}, ensure_ascii=False)}\n\n"
                    # Call events
                    for cid, call in list(call_tracker.items()):
                        if call['chat_id'] == chat_id and (call['caller_id'] == eff_id or call['callee_id'] == eff_id):
                            for evt in list(call['signal_events']):
                                if evt['event'] == 'incoming_call' and call['callee_id'] == eff_id:
                                    yield f"event: call\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
                                    call['signal_events'].remove(evt)
                                elif evt['event'] in ('call_accepted', 'call_rejected') and call['caller_id'] == eff_id:
                                    yield f"event: call\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
                                    call['signal_events'].remove(evt)
                                elif evt['event'] == 'signal' and evt.get('from') != eff_id:
                                    yield f"event: call\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
                                    call['signal_events'].remove(evt)
                                elif evt['event'] == 'call_ended':
                                    yield f"event: call\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
                                    call['signal_events'].remove(evt)
                            if call['state'] == 'ended' and not call['signal_events']:
                                del call_tracker[cid]
                except Exception as e:
                    logger.error(f'SSE error for chat {chat_id}: {e}')
                time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception as e:
            logger.error(f'SSE fatal error for chat {chat_id}: {e}')
        finally:
            if db:
                try: db.close()
                except: pass
    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp

@app.route('/api/chats')
def api_chats():
    if 'user_id' not in session:
        return jsonify({'error': 'not logged in'}), 401
    user_id = effective_user_id()
    db = get_db()
    chats = db.execute('''
        SELECT c.id, c.name, c.type,
            (SELECT content FROM messages WHERE chat_id = c.id AND deleted = 0 ORDER BY id DESC LIMIT 1) as last_msg,
            (SELECT file_type FROM messages WHERE chat_id = c.id AND deleted = 0 ORDER BY id DESC LIMIT 1) as last_msg_file_type,
            (SELECT id FROM messages WHERE chat_id = c.id AND deleted = 0 ORDER BY id DESC LIMIT 1) as last_msg_id
        FROM chats c
        JOIN chat_members cm ON c.id = cm.chat_id
        WHERE cm.user_id = ?
        ORDER BY COALESCE((SELECT id FROM messages WHERE chat_id = c.id AND deleted = 0 ORDER BY id DESC LIMIT 1), 0) DESC
    ''', (user_id,)).fetchall()
    result = []
    for c in chats:
        c = dict(c)
        if c['type'] == 'private':
            other = db.execute('''
                SELECT u.username, u.last_seen FROM users u
                JOIN chat_members cm ON u.id = cm.user_id
                WHERE cm.chat_id = ? AND cm.user_id != ? AND cm.user_id != 999
            ''', (c['id'], user_id)).fetchone()
            c['display_name'] = other['username'] if other else 'Чат'
            c['other_online'] = is_online(other['last_seen']) if other else False
        else:
            c['display_name'] = c['name']
            c['other_online'] = False
        result.append(c)
    users = db.execute('SELECT id, username, last_seen FROM users').fetchall()
    for u in users:
        u = dict(u)
        u['online'] = is_online(u['last_seen'])
    db.close()
    return jsonify({'chats': result, 'users': [dict(u) for u in users]})

@app.route('/health')
def health():
    return 'ok'

@app.errorhandler(Exception)
def handle_error(e):
    import traceback
    if request.path.startswith('/api/'):
        return jsonify({'error': str(e)}), 500
    return f'<pre>{traceback.format_exc()}</pre>', 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
