import os
import re
import time
import secrets
from datetime import timedelta
from contextlib import contextmanager
from functools import wraps

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json
from flask import (Flask, render_template, request, jsonify, session,
                   redirect, url_for, send_from_directory)

from i18n import LANGS, DEFAULT_LANG, STRINGS, translate

app = Flask(__name__)
# Задай SECRET_KEY в .env, иначе при каждом перезапуске все будут выходить из аккаунта
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
app.permanent_session_lifetime = 60 * 60 * 24 * 30

DB_HOST = os.environ.get('DB_HOST', 'db')
DB_NAME = os.environ.get('POSTGRES_DB', 'family_db')
DB_USER = os.environ.get('POSTGRES_USER', 'dastan')
DB_PASS = os.environ.get('POSTGRES_PASSWORD', 'supersecret')
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', '/app/uploads')
TZ = 'Asia/Qyzylorda'
THEMES = {'kawaii', 'cyberpunk', 'minecraft'}   # + файл static/themes/<имя>.svg и CSS-класс theme-<имя>

os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------- БД ----------
def get_conn():
    return psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)


@contextmanager
def db():
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    while True:
        try:
            with db() as cur:
                cur.execute('''CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY, name VARCHAR(50) NOT NULL, pin_code VARCHAR(4),
                    role VARCHAR(10) NOT NULL, avatar VARCHAR(10), balance INTEGER DEFAULT 0,
                    theme VARCHAR(20) DEFAULT 'kawaii', dark_mode BOOLEAN DEFAULT FALSE);''')
                cur.execute('''CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL, reward INTEGER NOT NULL);''')
                cur.execute('''CREATE TABLE IF NOT EXISTS rewards (
                    id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL, cost INTEGER NOT NULL,
                    icon VARCHAR(10) DEFAULT '🎁');''')
                cur.execute('''CREATE TABLE IF NOT EXISTS history (
                    id SERIAL PRIMARY KEY, event_desc TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
                cur.execute('''CREATE TABLE IF NOT EXISTS fines (
                    id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL, amount INTEGER NOT NULL);''')
                # Миграции для уже существующей базы (безопасно запускать повторно)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS age INTEGER;")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS gender VARCHAR(10);")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_img VARCHAR(100);")
                cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS actor VARCHAR(50);")
                cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS action TEXT;")
                cur.execute("ALTER TABLE history ADD COLUMN IF NOT EXISTS params JSONB;")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS lang VARCHAR(5);")
                # расписание задач: daily / weekly (дни в weekdays: 0=Пн … 6=Вс) / once; assignee_id NULL = всем детям
                cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS kind VARCHAR(10) DEFAULT 'daily';")
                cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS weekdays VARCHAR(20);")
                cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignee_id INTEGER REFERENCES users(id) ON DELETE CASCADE;")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS goal_reward_id INTEGER REFERENCES rewards(id) ON DELETE SET NULL;")
                cur.execute('''CREATE TABLE IF NOT EXISTS completions (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    task_name VARCHAR(100) NOT NULL, reward INTEGER NOT NULL, for_date DATE NOT NULL,
                    status VARCHAR(10) NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT (now() AT TIME ZONE 'UTC'), decided_at TIMESTAMP);''')
                cur.execute('''CREATE TABLE IF NOT EXISTS purchases (
                    id SERIAL PRIMARY KEY,
                    reward_id INTEGER REFERENCES rewards(id) ON DELETE SET NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    reward_name VARCHAR(100) NOT NULL, icon VARCHAR(10), cost INTEGER NOT NULL,
                    status VARCHAR(10) NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT (now() AT TIME ZONE 'UTC'), decided_at TIMESTAMP);''')
                cur.execute('''CREATE TABLE IF NOT EXISTS streak_bonuses (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    day DATE NOT NULL, amount INTEGER NOT NULL, PRIMARY KEY (user_id, day));''')
                cur.execute("CREATE TABLE IF NOT EXISTS settings (key VARCHAR(30) PRIMARY KEY, value VARCHAR(30) NOT NULL);")
                cur.execute("INSERT INTO settings VALUES ('streak_days', '5'), ('streak_bonus', '10') ON CONFLICT DO NOTHING;")

                cur.execute('SELECT COUNT(*) FROM users;')
                if cur.fetchone()[0] == 0:
                    for n, p, r, a, g in [('Папа', '9999', 'admin', '👨‍💻', 'male'),
                                          ('Амина', '1111', 'kid', '👧', 'female'),
                                          ('Мадина', '2222', 'kid', '👱‍♀️', 'female'),
                                          ('Камила', '3333', 'kid', '👧🏻', 'female')]:
                        cur.execute("INSERT INTO users (name, pin_code, role, avatar, gender) VALUES (%s,%s,%s,%s,%s)",
                                    (n, p, r, a, g))
                    cur.execute("INSERT INTO tasks (name, reward) VALUES ('Проснуться в 7:00 без капризов', 5), ('Помощь с посудой', 10)")
                    cur.execute("INSERT INTO rewards (name, cost, icon) VALUES ('Выбрать фильм на вечер', 30, '🎬'), ('Лечь спать на 30 мин позже', 50, '🌙')")
                    cur.execute("INSERT INTO fines (name, amount) VALUES ('Не убрал(а) за собой', 5)")
            print("База данных успешно инициализирована!")
            break
        except Exception as e:
            print(f"База данных еще запускается, ждем 2 секунды... ({e})")
            time.sleep(2)


init_db()


# ---------- Вспомогательное ----------
def cur_lang():
    lang = session.get('lang')
    return lang if lang in LANGS else DEFAULT_LANG


def tr(key, **kw):
    return translate(cur_lang(), key, **kw)


class Bad(Exception):
    """Ошибка проверки данных. В сообщении — ключ перевода (err.*)."""


def log(cur, key, actor=None, **params):
    """Запись в журнал: кто (actor), что (ключ перевода + параметры), когда (UTC).
    Текст собирается при показе — на языке того, кто смотрит журнал. event_desc — русская копия для совместимости."""
    actor = actor or session.get('name') or '—'
    cur.execute("INSERT INTO history (event_desc, actor, action, params, created_at) "
                "VALUES (%s, %s, %s, %s, now() AT TIME ZONE 'UTC')",
                (translate('ru', key, **params), actor, key, Json(params)))


def api(admin=False):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            if 'user_id' not in session:
                return jsonify(success=False, error=tr('err.need_login')), 401
            if admin and session.get('role') != 'admin':
                return jsonify(success=False, error=tr('err.admin_only')), 403
            try:
                return f(*a, **k)
            except Bad as e:
                return jsonify(success=False, error=tr(str(e))), 400
            except (ValueError, TypeError, KeyError):
                return jsonify(success=False, error=tr('err.bad_data')), 400
        return wrapper
    return deco


def body():
    return request.get_json(silent=True) or {}


def default_avatar(role, gender):
    return {('kid', 'female'): '👧', ('kid', 'male'): '👦',
            ('admin', 'female'): '👩', ('admin', 'male'): '👨'}.get((role, gender), '🙂')


def clean_user(d, user_id=-1):
    """Проверка профиля участника. user_id — чтобы не считать свой же ПИН дубликатом."""
    name = (d.get('name') or '').strip()[:50]
    pin = str(d.get('pin') or '')
    role = d.get('role') or 'kid'
    gender = d.get('gender') or None
    age = int(d['age']) if d.get('age') not in (None, '') else None
    if not name:
        raise Bad('err.enter_name')
    if not re.fullmatch(r'\d{4}', pin):
        raise Bad('err.pin_4')
    if role not in ('kid', 'admin'):
        raise Bad('err.bad_role')
    if gender not in (None, 'male', 'female'):
        raise Bad('err.bad_gender')
    if age is not None and not 0 <= age <= 120:
        raise Bad('err.bad_age')
    with db() as cur:
        cur.execute("SELECT 1 FROM users WHERE pin_code = %s AND id <> %s", (pin, user_id))
        if cur.fetchone():
            raise Bad('err.pin_taken')
    avatar = (d.get('avatar') or '').strip()[:10] or default_avatar(role, gender)
    return name, pin, role, gender, age, avatar


def delete_avatar_file(filename):
    if filename:
        try:
            os.remove(os.path.join(UPLOAD_DIR, os.path.basename(filename)))
        except OSError:
            pass


def require_kid():
    if session.get('role') != 'kid':
        raise Bad('err.no_access')


def today_date(cur):
    """Сегодняшняя дата в часовом поясе семьи (а не сервера)."""
    cur.execute("SELECT (now() AT TIME ZONE %s)::date", (TZ,))
    return cur.fetchone()[0]


def get_setting(cur, key, default):
    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
    row = cur.fetchone()
    try:
        return int(row[0]) if row else default
    except ValueError:
        return default


def weekday_list(text):
    return [int(x) for x in (text or '').split(',') if x.strip().isdigit()]


def streak_ending(cur, uid, end_day):
    """Сколько дней подряд (заканчивая end_day) у ребёнка есть хотя бы одна подтверждённая задача."""
    cur.execute("SELECT DISTINCT for_date FROM completions WHERE user_id = %s AND status = 'approved' "
                "AND for_date <= %s ORDER BY for_date DESC LIMIT 400", (uid, end_day))
    n, expected = 0, end_day
    for row in cur.fetchall():
        if row[0] != expected:
            break
        n += 1
        expected -= timedelta(days=1)
    return n


def current_streak(cur, uid, today):
    """Серия жива, если последний день — сегодня или вчера."""
    return streak_ending(cur, uid, today) or streak_ending(cur, uid, today - timedelta(days=1))


def grant_completion(cur, comp_id):
    """Подтверждает заявку: начисляет монеты и, если набрана серия, — бонус. Всё в одной транзакции."""
    cur.execute("UPDATE completions SET status = 'approved', decided_at = now() AT TIME ZONE 'UTC' "
                "WHERE id = %s AND status = 'pending' RETURNING user_id, task_name, reward, for_date", (comp_id,))
    c = cur.fetchone()
    if not c:
        raise Bad('err.request_done')
    cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s RETURNING name", (c['reward'], c['user_id']))
    kid = cur.fetchone()['name']
    log(cur, 'log.completion_approved', task=c['task_name'], kid=kid, reward=c['reward'])
    days, bonus = get_setting(cur, 'streak_days', 5), get_setting(cur, 'streak_bonus', 10)
    if days > 0 and bonus > 0:
        n = streak_ending(cur, c['user_id'], c['for_date'])
        if n > 0 and n % days == 0:
            cur.execute("INSERT INTO streak_bonuses (user_id, day, amount) VALUES (%s, %s, %s) "
                        "ON CONFLICT DO NOTHING RETURNING 1", (c['user_id'], c['for_date'], bonus))
            if cur.fetchone():   # один бонус на один день серии, даже если подтверждено несколько задач
                cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (bonus, c['user_id']))
                log(cur, 'log.streak_bonus', kid=kid, days=n, amount=bonus)
    return kid


def kid_tasks(cur, uid, today):
    """Задачи, доступные ребёнку сегодня, со статусом: None / pending / approved."""
    cur.execute("""SELECT t.id, t.name, t.reward, t.kind, t.weekdays,
                          (SELECT c.status FROM completions c
                            WHERE c.task_id = t.id AND c.user_id = %s AND c.status IN ('pending', 'approved')
                              AND (t.kind = 'once' OR c.for_date = %s) ORDER BY c.id DESC LIMIT 1) AS state
                   FROM tasks t WHERE t.assignee_id IS NULL OR t.assignee_id = %s ORDER BY t.id""", (uid, today, uid))
    out = []
    for t in cur.fetchall():
        if t['kind'] == 'weekly' and today.weekday() not in weekday_list(t['weekdays']):
            continue
        if t['kind'] == 'once' and t['state'] == 'approved':
            continue   # разовая задача выполнена навсегда
        out.append(dict(t))
    return out


# ---------- Страницы ----------
@app.route('/')
def index():
    if 'user_id' not in session:
        return render_template('index.html', me=None)

    with db() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (session['user_id'],))
        me = cur.fetchone()
        if not me:
            session.clear()
            return redirect(url_for('index'))
        session['role'], session['name'] = me['role'], me['name']

        cur.execute("SELECT * FROM rewards ORDER BY cost, id")
        rewards = cur.fetchall()
        ctx = dict(me=me, rewards=rewards, members=[], kids=[], tasks=[], fines=[], history=[])

        if me['role'] == 'admin':   # ПИНы и логи уходят в браузер только взрослым
            cur.execute("SELECT * FROM users ORDER BY (role = 'admin') DESC, id")
            ctx['members'] = [dict(r) for r in cur.fetchall()]
            ctx['kids'] = [m for m in ctx['members'] if m['role'] == 'kid']
            cur.execute("SELECT t.*, u.name AS assignee_name FROM tasks t "
                        "LEFT JOIN users u ON u.id = t.assignee_id ORDER BY t.id")
            ctx['tasks'] = [dict(r, days=weekday_list(r['weekdays'])) for r in cur.fetchall()]
            fmt = "to_char((%s AT TIME ZONE 'UTC') AT TIME ZONE %%s, 'DD.MM HH24:MI')"
            cur.execute(f"""SELECT c.id, c.task_name, c.reward, u.name AS kid, {fmt % 'c.created_at'} AS ts
                            FROM completions c JOIN users u ON u.id = c.user_id
                            WHERE c.status = 'pending' ORDER BY c.id""", (TZ,))
            ctx['req_tasks'] = cur.fetchall()
            cur.execute(f"""SELECT p.id, p.reward_name, p.icon, p.cost, u.name AS kid, u.balance, {fmt % 'p.created_at'} AS ts
                            FROM purchases p JOIN users u ON u.id = p.user_id
                            WHERE p.status = 'pending' ORDER BY p.id""", (TZ,))
            ctx['req_buys'] = cur.fetchall()
            ctx['pending_count'] = len(ctx['req_tasks']) + len(ctx['req_buys'])
            ctx['streak'] = dict(days=get_setting(cur, 'streak_days', 5), bonus=get_setting(cur, 'streak_bonus', 10))
            cur.execute("SELECT * FROM fines ORDER BY id")
            ctx['fines'] = cur.fetchall()
            cur.execute("""SELECT actor, action, event_desc, params,
                                  to_char((created_at AT TIME ZONE 'UTC') AT TIME ZONE %s, 'DD.MM HH24:MI') AS ts
                           FROM history ORDER BY id DESC LIMIT 200""", (TZ,))
            # новые записи (есть params) собираем на языке зрителя; старые показываем как сохранены
            ctx['history'] = [dict(actor=h['actor'], ts=h['ts'],
                                   text=tr(h['action'], **h['params']) if h['params'] is not None
                                   else (h['action'] or h['event_desc']))
                              for h in cur.fetchall()]
        else:
            uid, today = me['id'], today_date(cur)
            days, bonus = get_setting(cur, 'streak_days', 5), get_setting(cur, 'streak_bonus', 10)
            n = current_streak(cur, uid, today)
            ctx.update(my_tasks=kid_tasks(cur, uid, today), streak_now=n, streak_bonus=bonus if days > 0 else 0,
                       streak_left=(days - n % days) if days > 0 else 0)
            cur.execute("SELECT COALESCE(SUM(cost), 0) FROM purchases WHERE user_id = %s AND status = 'pending'", (uid,))
            ctx['reserved'] = cur.fetchone()[0]
            cur.execute("SELECT id, name, icon, cost FROM rewards WHERE id = %s", (me['goal_reward_id'],))
            goal = cur.fetchone()
            ctx['goal'] = dict(goal, pct=min(100, int(me['balance'] * 100 / goal['cost']))) if goal else None
            fmt = "to_char((created_at AT TIME ZONE 'UTC') AT TIME ZONE %s, 'DD.MM HH24:MI')"
            cur.execute(f"""SELECT 'task' AS type, id, task_name AS name, '' AS icon, reward AS amount, status, created_at,
                                   {fmt} AS ts FROM completions WHERE user_id = %s ORDER BY id DESC LIMIT 8""", (TZ, uid))
            reqs = [dict(r) for r in cur.fetchall()]
            cur.execute(f"""SELECT 'buy' AS type, id, reward_name AS name, COALESCE(icon, '') AS icon, cost AS amount, status,
                                   created_at, {fmt} AS ts FROM purchases WHERE user_id = %s ORDER BY id DESC LIMIT 8""", (TZ, uid))
            reqs += [dict(r) for r in cur.fetchall()]
            ctx['my_requests'] = sorted(reqs, key=lambda r: r['created_at'], reverse=True)[:10]
    return render_template('index.html', **ctx)


@app.route('/login', methods=['POST'])
def login():
    pin = request.form.get('pin', '')
    with db() as cur:
        cur.execute("SELECT * FROM users WHERE pin_code = %s", (pin,))
        user = cur.fetchone()
        if user:
            # язык, выбранный на экране входа, важнее сохранённого; иначе берём сохранённый
            lang = session.get('lang') if session.get('lang') in LANGS else (user['lang'] or DEFAULT_LANG)
            cur.execute("UPDATE users SET lang = %s WHERE id = %s", (lang, user['id']))
            log(cur, 'log.login', actor=user['name'])
    if not user:
        return f"{tr('login.wrong_pin')} <a href='/'>{tr('login.back')}</a>", 401
    session.clear()
    session.permanent = True
    session.update(user_id=user['id'], role=user['role'], name=user['name'], lang=lang)
    return redirect(url_for('index'))


@app.route('/set_lang', methods=['POST'])
def set_lang():
    lang = body().get('lang')
    if lang not in LANGS:
        return jsonify(success=False), 400
    session['lang'] = lang
    session.permanent = True
    if 'user_id' in session:
        with db() as cur:
            cur.execute("UPDATE users SET lang = %s WHERE id = %s", (lang, session['user_id']))
    return jsonify(success=True)


@app.context_processor
def inject_i18n():
    lang = cur_lang()
    return dict(t=tr, lang=lang, langs=LANGS, T=STRINGS[lang])


@app.route('/logout')
def logout():
    if 'user_id' in session:
        with db() as cur:
            log(cur, 'log.logout')
    session.clear()
    return redirect(url_for('index'))


@app.route('/avatars/<path:filename>')
def avatar_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------- Участники ----------
@app.route('/add_user', methods=['POST'])
@api(admin=True)
def add_user():
    name, pin, role, gender, age, avatar = clean_user(body())
    with db() as cur:
        cur.execute("INSERT INTO users (name, pin_code, role, gender, age, avatar, balance) "
                    "VALUES (%s,%s,%s,%s,%s,%s,0)", (name, pin, role, gender, age, avatar))
        log(cur, 'log.user_added_admin' if role == 'admin' else 'log.user_added_kid', name=name)
    return jsonify(success=True)


@app.route('/edit_user', methods=['POST'])
@api(admin=True)
def edit_user():
    d = body()
    uid = int(d['id'])
    name, pin, role, gender, age, avatar = clean_user(d, uid)
    balance = int(d.get('balance', 0))
    with db() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s", (uid,))
        old = cur.fetchone()
        if not old:
            raise Bad('err.user_not_found')
        if old['role'] == 'admin' and role != 'admin':
            cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND id <> %s", (uid,))
            if cur.fetchone()[0] == 0:
                raise Bad('err.last_admin')
        cur.execute("UPDATE users SET name=%s, pin_code=%s, role=%s, gender=%s, age=%s, avatar=%s, balance=%s "
                    "WHERE id=%s", (name, pin, role, gender, age, avatar, balance, uid))
        log(cur, 'log.user_edited', name=name)
    return jsonify(success=True)


@app.route('/delete_user', methods=['POST'])
@api(admin=True)
def delete_user():
    uid = int(body()['id'])
    if uid == session['user_id']:
        raise Bad('err.delete_self')
    with db() as cur:
        cur.execute("DELETE FROM users WHERE id = %s RETURNING name, avatar_img", (uid,))
        row = cur.fetchone()
        if not row:
            raise Bad('err.user_not_found')
        log(cur, 'log.user_deleted', name=row['name'])
    delete_avatar_file(row['avatar_img'])
    return jsonify(success=True)


@app.route('/upload_avatar', methods=['POST'])
@api()
def upload_avatar():
    uid = int(request.form.get('user_id') or session['user_id'])
    if uid != session['user_id'] and session.get('role') != 'admin':
        return jsonify(success=False, error=tr('err.no_access')), 403
    f = request.files.get('file')
    raw = f.read() if f else b''
    if not raw.startswith(b'\xff\xd8'):
        raise Bad('err.need_photo')
    filename = f"u{uid}_{int(time.time())}.jpg"
    with open(os.path.join(UPLOAD_DIR, filename), 'wb') as out:
        out.write(raw)
    with db() as cur:
        cur.execute("SELECT avatar_img, name FROM users WHERE id = %s", (uid,))
        old = cur.fetchone()
        if not old:
            delete_avatar_file(filename)
            raise Bad('err.user_not_found')
        cur.execute("UPDATE users SET avatar_img = %s WHERE id = %s", (filename, uid))
        log(cur, 'log.photo', name=old['name'])
    delete_avatar_file(old['avatar_img'])
    return jsonify(success=True)


@app.route('/update_settings', methods=['POST'])
@api()
def update_settings():
    d = body()
    theme = d.get('theme')
    if theme not in THEMES:
        raise Bad('err.bad_theme')
    with db() as cur:
        cur.execute("UPDATE users SET theme = %s, dark_mode = %s WHERE id = %s",
                    (theme, bool(d.get('dark_mode')), session['user_id']))
    return jsonify(success=True)


# ---------- Задачи, монеты ----------
@app.route('/add_points', methods=['POST'])
@api(admin=True)
def add_points():
    """Быстрое начисление взрослым: сразу подтверждённое выполнение, без заявки."""
    d = body()
    with db() as cur:
        cur.execute("SELECT id, name, reward FROM tasks WHERE id = %s", (int(d['task_id']),))
        task = cur.fetchone()
        if not task:
            raise Bad('err.task_not_found')
        cur.execute("SELECT 1 FROM users WHERE id = %s AND role = 'kid'", (int(d['kid_id']),))
        if not cur.fetchone():
            raise Bad('err.user_not_found')
        cur.execute("INSERT INTO completions (task_id, user_id, task_name, reward, for_date, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id",
                    (task['id'], int(d['kid_id']), task['name'], task['reward'], today_date(cur)))
        grant_completion(cur, cur.fetchone()[0])
    return jsonify(success=True)


@app.route('/add_task', methods=['POST'])
@api(admin=True)
def add_task():
    d = body()
    name, reward = (d.get('name') or '').strip()[:100], int(d.get('reward', 0))
    kind = d.get('kind') or 'daily'
    if not name or reward <= 0:
        raise Bad('err.task_fields')
    if kind not in ('daily', 'weekly', 'once'):
        raise Bad('err.bad_kind')
    days = sorted({int(x) for x in (d.get('weekdays') or []) if 0 <= int(x) <= 6}) if kind == 'weekly' else []
    if kind == 'weekly' and not days:
        raise Bad('err.weekdays')
    assignee = int(d['assignee_id']) if d.get('assignee_id') not in (None, '') else None
    with db() as cur:
        if assignee is not None:
            cur.execute("SELECT 1 FROM users WHERE id = %s AND role = 'kid'", (assignee,))
            if not cur.fetchone():
                raise Bad('err.bad_assignee')
        cur.execute("INSERT INTO tasks (name, reward, kind, weekdays, assignee_id) VALUES (%s, %s, %s, %s, %s)",
                    (name, reward, kind, ','.join(map(str, days)) or None, assignee))
        log(cur, 'log.task_added', name=name, reward=reward)
    return jsonify(success=True)


# ---------- Заявки: выполнение задач ----------
@app.route('/submit_task', methods=['POST'])
@api()
def submit_task():
    require_kid()
    tid, uid = int(body()['task_id']), session['user_id']
    with db() as cur:
        today = today_date(cur)
        cur.execute("SELECT * FROM tasks WHERE id = %s", (tid,))
        task = cur.fetchone()
        if (not task or task['assignee_id'] not in (None, uid)
                or (task['kind'] == 'weekly' and today.weekday() not in weekday_list(task['weekdays']))):
            raise Bad('err.task_unavailable')
        # одним запросом: нельзя отправить повторно, пока заявка ждёт проверки или уже подтверждена
        cur.execute("""INSERT INTO completions (task_id, user_id, task_name, reward, for_date, status)
                       SELECT %s, %s, %s, %s, %s, 'pending'
                       WHERE NOT EXISTS (SELECT 1 FROM completions WHERE task_id = %s AND user_id = %s
                                         AND status IN ('pending', 'approved') AND (%s OR for_date = %s))
                       RETURNING id""",
                    (tid, uid, task['name'], task['reward'], today, tid, uid, task['kind'] == 'once', today))
        if not cur.fetchone():
            raise Bad('err.already_sent')
        log(cur, 'log.task_submitted', task=task['name'], reward=task['reward'])
    return jsonify(success=True)


@app.route('/decide_completion', methods=['POST'])
@api(admin=True)
def decide_completion():
    d = body()
    cid = int(d['id'])
    with db() as cur:
        if d.get('approve'):
            grant_completion(cur, cid)
        else:
            cur.execute("UPDATE completions SET status = 'rejected', decided_at = now() AT TIME ZONE 'UTC' "
                        "WHERE id = %s AND status = 'pending' RETURNING user_id, task_name", (cid,))
            row = cur.fetchone()
            if not row:
                raise Bad('err.request_done')
            cur.execute("SELECT name FROM users WHERE id = %s", (row['user_id'],))
            log(cur, 'log.completion_rejected', task=row['task_name'], kid=cur.fetchone()['name'])
    return jsonify(success=True)


# ---------- Серии и цель накопления ----------
@app.route('/save_streak', methods=['POST'])
@api(admin=True)
def save_streak():
    d = body()
    days, bonus = int(d['days']), int(d['bonus'])
    if not (1 <= days <= 30 and 0 <= bonus <= 1000):
        raise Bad('err.bad_streak')
    with db() as cur:
        for key, value in (('streak_days', days), ('streak_bonus', bonus)):
            cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, str(value)))
        log(cur, 'log.streak_settings', days=days, bonus=bonus)
    return jsonify(success=True)


@app.route('/set_goal', methods=['POST'])
@api()
def set_goal():
    require_kid()
    rid = body().get('reward_id')
    with db() as cur:
        if rid in (None, ''):
            cur.execute("UPDATE users SET goal_reward_id = NULL WHERE id = %s", (session['user_id'],))
            log(cur, 'log.goal_cleared')
        else:
            cur.execute("SELECT name FROM rewards WHERE id = %s", (int(rid),))
            reward = cur.fetchone()
            if not reward:
                raise Bad('err.reward_not_found')
            cur.execute("UPDATE users SET goal_reward_id = %s WHERE id = %s", (int(rid), session['user_id']))
            log(cur, 'log.goal_set', name=reward['name'])
    return jsonify(success=True)


@app.route('/pending_count')
@api(admin=True)
def pending_count():
    with db() as cur:
        cur.execute("SELECT (SELECT COUNT(*) FROM completions WHERE status = 'pending') + "
                    "(SELECT COUNT(*) FROM purchases WHERE status = 'pending')")
        return jsonify(success=True, count=cur.fetchone()[0])


@app.route('/delete_task', methods=['POST'])
@api(admin=True)
def delete_task():
    with db() as cur:
        cur.execute("DELETE FROM tasks WHERE id = %s RETURNING name", (int(body()['id']),))
        row = cur.fetchone()
        if row:
            log(cur, 'log.task_deleted', name=row['name'])
    return jsonify(success=True)


# ---------- Штрафы ----------
@app.route('/add_fine', methods=['POST'])
@api(admin=True)
def add_fine():
    d = body()
    name, amount = (d.get('name') or '').strip()[:100], int(d.get('amount', 0))
    if not name or amount <= 0:
        raise Bad('err.fine_fields')
    with db() as cur:
        cur.execute("INSERT INTO fines (name, amount) VALUES (%s, %s)", (name, amount))
        log(cur, 'log.fine_created', name=name, amount=amount)
    return jsonify(success=True)


@app.route('/delete_fine', methods=['POST'])
@api(admin=True)
def delete_fine():
    with db() as cur:
        cur.execute("DELETE FROM fines WHERE id = %s RETURNING name", (int(body()['id']),))
        row = cur.fetchone()
        if row:
            log(cur, 'log.fine_deleted', name=row['name'])
    return jsonify(success=True)


@app.route('/apply_fine', methods=['POST'])
@api(admin=True)
def apply_fine():
    d = body()
    with db() as cur:
        cur.execute("SELECT name, amount FROM fines WHERE id = %s", (int(d['fine_id']),))
        fine = cur.fetchone()
        cur.execute("SELECT name, balance FROM users WHERE id = %s", (int(d['user_id']),))
        user = cur.fetchone()
        if not fine or not user:
            raise Bad('err.not_found')
        taken = min(fine['amount'], user['balance'])   # баланс не уходит в минус
        cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (taken, int(d['user_id'])))
        log(cur, 'log.fine_applied', fine=fine['name'], user=user['name'], taken=taken)
    return jsonify(success=True)


# ---------- Магазин ----------
@app.route('/add_reward', methods=['POST'])
@api(admin=True)
def add_reward():
    d = body()
    name, cost = (d.get('name') or '').strip()[:100], int(d.get('cost', 0))
    icon = (d.get('icon') or '🎁').strip()[:10]
    if not name or cost <= 0:
        raise Bad('err.reward_fields')
    with db() as cur:
        cur.execute("INSERT INTO rewards (name, cost, icon) VALUES (%s,%s,%s)", (name, cost, icon))
        log(cur, 'log.reward_added', icon=icon, name=name, cost=cost)
    return jsonify(success=True)


@app.route('/delete_reward', methods=['POST'])
@api(admin=True)
def delete_reward():
    with db() as cur:
        cur.execute("DELETE FROM rewards WHERE id = %s RETURNING name", (int(body()['id']),))
        row = cur.fetchone()
        if row:
            log(cur, 'log.reward_deleted', name=row['name'])
    return jsonify(success=True)


@app.route('/buy_reward', methods=['POST'])
@api()
def buy_reward():
    """Ребёнок отправляет заявку. Монеты списываются только после подтверждения взрослым."""
    require_kid()
    uid = session['user_id']
    with db() as cur:
        cur.execute("SELECT name, cost, icon FROM rewards WHERE id = %s", (int(body()['reward_id']),))
        reward = cur.fetchone()
        if not reward:
            raise Bad('err.reward_not_found')
        cur.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (uid,))   # блокируем строку: без гонок
        balance = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(cost), 0) FROM purchases WHERE user_id = %s AND status = 'pending'", (uid,))
        if balance - cur.fetchone()[0] < reward['cost']:   # монеты под другие заявки считаются занятыми
            raise Bad('err.no_coins_free')
        cur.execute("INSERT INTO purchases (reward_id, user_id, reward_name, icon, cost) "
                    "SELECT id, %s, name, icon, cost FROM rewards WHERE id = %s", (uid, int(body()['reward_id'])))
        log(cur, 'log.purchase_requested', name=reward['name'], cost=reward['cost'])
    return jsonify(success=True)


@app.route('/cancel_purchase', methods=['POST'])
@api()
def cancel_purchase():
    require_kid()
    with db() as cur:
        cur.execute("UPDATE purchases SET status = 'cancelled', decided_at = now() AT TIME ZONE 'UTC' "
                    "WHERE id = %s AND user_id = %s AND status = 'pending' RETURNING reward_name",
                    (int(body()['id']), session['user_id']))
        row = cur.fetchone()
        if not row:
            raise Bad('err.request_done')
        log(cur, 'log.purchase_cancelled', name=row['reward_name'])
    return jsonify(success=True)


@app.route('/decide_purchase', methods=['POST'])
@api(admin=True)
def decide_purchase():
    d = body()
    pid, approve = int(d['id']), bool(d.get('approve'))
    with db() as cur:
        cur.execute("UPDATE purchases SET status = %s, decided_at = now() AT TIME ZONE 'UTC' "
                    "WHERE id = %s AND status = 'pending' RETURNING user_id, reward_id, reward_name, cost",
                    ('approved' if approve else 'rejected', pid))
        p = cur.fetchone()
        if not p:
            raise Bad('err.request_done')
        if approve:
            cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s AND balance >= %s RETURNING name",
                        (p['cost'], p['user_id'], p['cost']))
            kid = cur.fetchone()
            if not kid:
                raise Bad('err.no_coins')   # исключение откатывает всю транзакцию: заявка останется ожидающей
            cur.execute("UPDATE users SET goal_reward_id = NULL WHERE id = %s AND goal_reward_id = %s",
                        (p['user_id'], p['reward_id']))   # цель достигнута
            log(cur, 'log.purchase_approved', kid=kid['name'], name=p['reward_name'], cost=p['cost'])
        else:
            cur.execute("SELECT name FROM users WHERE id = %s", (p['user_id'],))
            log(cur, 'log.purchase_rejected', kid=cur.fetchone()['name'], name=p['reward_name'])
    return jsonify(success=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
