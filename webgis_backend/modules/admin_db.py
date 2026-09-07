# -*- coding: utf-8 -*-
"""
系统管理数据库：用户 / 角色 / 权限 / 操作日志（SQLite）。
首次启动自动建表并写入种子数据（权限点、内置角色、管理员账号）。
"""
import os
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

DB_RELPATH = os.path.join('data', 'processed', 'db', 'admin.db')

# 权限点定义（名称, 编码, 说明）
PERMISSIONS = [
    ('用户管理', 'user:view', '查看用户列表'),
    ('用户管理', 'user:create', '新增用户'),
    ('用户管理', 'user:update', '编辑用户'),
    ('用户管理', 'user:delete', '删除用户'),
    ('角色管理', 'role:view', '查看角色列表'),
    ('角色管理', 'role:create', '新增角色'),
    ('角色管理', 'role:update', '编辑角色'),
    ('角色管理', 'role:delete', '删除角色'),
    ('权限管理', 'permission:view', '查看权限列表'),
    ('权限管理', 'permission:assign', '为角色配置权限'),
    ('系统日志', 'log:view', '查看系统日志'),
    ('系统设置', 'setting:edit', '修改系统设置/密码'),
    ('系统状态', 'system:view', '查看系统状态'),
    ('数据管理', 'data:manage', '管理数据文件（上传/删除）'),
]

# 内置角色及其权限
ROLES = [
    ('系统管理员', 'admin', '拥有全部权限', [p[1] for p in PERMISSIONS]),
    ('运维人员', 'operator', '负责日常运维与数据管理',
     ['user:view', 'user:create', 'user:update', 'role:view',
      'role:create', 'role:update', 'permission:view', 'permission:assign',
      'log:view', 'setting:edit', 'system:view']),
    ('访客', 'viewer', '只读查看',
     ['user:view', 'role:view', 'permission:view', 'log:view', 'system:view']),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    role_id INTEGER,
    status INTEGER DEFAULT 1,
    created_at TEXT,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL,
    permission_id INTEGER NOT NULL,
    PRIMARY KEY (role_id, permission_id)
);
CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    action TEXT,
    detail TEXT,
    ip TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""


def db_path(base_dir):
    return os.path.join(base_dir, *DB_RELPATH.split(os.sep))


def connect(base_dir):
    conn = sqlite3.connect(db_path(base_dir))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def init_admin_db(base_dir, default_users=None):
    """建表并写入种子数据（幂等：已存在则跳过）。

    default_users: [{username, password, display_name, role_code}, ...]
    未指定时回退为仅管理员账号。
    """
    os.makedirs(os.path.dirname(db_path(base_dir)), exist_ok=True)
    if not default_users:
        default_users = [{'username': 'admin', 'password': 'admin123',
                          'display_name': '系统管理员', 'role_code': 'admin'}]
    conn = connect(base_dir)
    try:
        conn.executescript(_SCHEMA)
        # 权限点
        if conn.execute('SELECT COUNT(*) FROM permissions').fetchone()[0] == 0:
            conn.executemany(
                'INSERT INTO permissions (name, code, description) VALUES (?,?,?)',
                PERMISSIONS)
        # 角色
        if conn.execute('SELECT COUNT(*) FROM roles').fetchone()[0] == 0:
            for name, code, desc, perms in ROLES:
                cur = conn.execute(
                    'INSERT INTO roles (name, code, description, created_at) VALUES (?,?,?,?)',
                    (name, code, desc, _now()))
                role_id = cur.lastrowid
                for pc in perms:
                    pid = conn.execute(
                        'SELECT id FROM permissions WHERE code=?', (pc,)).fetchone()
                    if pid:
                        conn.execute(
                            'INSERT INTO role_permissions (role_id, permission_id) VALUES (?,?)',
                            (role_id, pid['id']))
        # 默认账号：按用户名补插（已有则跳过，不覆盖密码）
        for u in default_users:
            exists = conn.execute(
                'SELECT id FROM users WHERE username=?', (u['username'],)).fetchone()
            if exists:
                continue
            role = conn.execute(
                'SELECT id FROM roles WHERE code=?', (u['role_code'],)).fetchone()
            conn.execute(
                'INSERT INTO users (username, password_hash, display_name, role_id, status, created_at) '
                'VALUES (?,?,?,?,1,?)',
                (u['username'], generate_password_hash(u['password']),
                 u['display_name'], role['id'] if role else None, _now()))
        # 权限点/角色授权增量补种（老库升级：新增权限点自动授予 admin/operator）
        for name, code, desc in PERMISSIONS:
            row = conn.execute('SELECT id FROM permissions WHERE code=?', (code,)).fetchone()
            if row is None:
                cur = conn.execute(
                    'INSERT INTO permissions (name, code, description) VALUES (?,?,?)',
                    (name, code, desc))
                pid = cur.lastrowid
            else:
                pid = row['id']
            for role_code in ('admin', 'operator'):
                r = conn.execute('SELECT id FROM roles WHERE code=?', (role_code,)).fetchone()
                if r:
                    conn.execute(
                        'INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)',
                        (r['id'], pid))
        conn.commit()
    finally:
        conn.close()


# ---------------- 用户 ----------------
def get_user_by_username(base_dir, username):
    conn = connect(base_dir)
    try:
        row = conn.execute(
            'SELECT u.*, r.name AS role_name, r.code AS role_code '
            'FROM users u LEFT JOIN roles r ON u.role_id = r.id '
            'WHERE u.username=?', (username,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users(base_dir):
    conn = connect(base_dir)
    try:
        rows = conn.execute(
            'SELECT u.id, u.username, u.display_name, u.status, u.created_at, u.last_login, '
            'r.name AS role_name, r.code AS role_code '
            'FROM users u LEFT JOIN roles r ON u.role_id = r.id ORDER BY u.id').fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_users(base_dir, page=1, page_size=10, keyword=None):
    """分页 + 关键词搜索用户。"""
    conn = connect(base_dir)
    try:
        where = ''
        params = []
        if keyword:
            where = ' WHERE (u.username LIKE ? OR u.display_name LIKE ?)'
            params = ['%' + keyword + '%', '%' + keyword + '%']
        total = conn.execute('SELECT COUNT(*) AS c FROM users u' + where, params).fetchone()['c']
        total_pages = max(1, (total + page_size - 1) // page_size)
        rows = conn.execute(
            'SELECT u.id, u.username, u.display_name, u.status, u.created_at, u.last_login, '
            'r.name AS role_name, r.code AS role_code '
            'FROM users u LEFT JOIN roles r ON u.role_id = r.id' + where +
            ' ORDER BY u.id LIMIT ? OFFSET ?',
            params + [page_size, (page - 1) * page_size]).fetchall()
        return {
            'items': [dict(r) for r in rows],
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
        }
    finally:
        conn.close()

def create_user(base_dir, username, password, display_name, role_id, status=1):
    conn = connect(base_dir)
    try:
        if conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            return None, '用户名已存在'
        conn.execute(
            'INSERT INTO users (username, password_hash, display_name, role_id, status, created_at) '
            'VALUES (?,?,?,?,?,?)',
            (username, generate_password_hash(password), display_name or username,
             role_id, 1 if status else 0, _now()))
        conn.commit()
        return True, 'ok'
    finally:
        conn.close()


def update_user(base_dir, user_id, display_name=None, role_id=None, status=None):
    conn = connect(base_dir)
    try:
        row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            return False, '用户不存在'
        new_name = display_name if display_name is not None else row['display_name']
        new_role = role_id if role_id is not None else row['role_id']
        new_status = status if status is not None else row['status']
        conn.execute(
            'UPDATE users SET display_name=?, role_id=?, status=? WHERE id=?',
            (new_name, new_role, 1 if new_status else 0, user_id))
        conn.commit()
        return True, 'ok'
    finally:
        conn.close()


def delete_user(base_dir, user_id):
    conn = connect(base_dir)
    try:
        row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            return False, '用户不存在'
        conn.execute('DELETE FROM users WHERE id=?', (user_id,))
        conn.commit()
        return True, 'ok'
    finally:
        conn.close()


def reset_password(base_dir, user_id, new_password):
    conn = connect(base_dir)
    try:
        if not conn.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone():
            return False, '用户不存在'
        conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                     (generate_password_hash(new_password), user_id))
        conn.commit()
        return True, 'ok'
    finally:
        conn.close()


def change_password(base_dir, user_id, old_password, new_password):
    conn = connect(base_dir)
    try:
        row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            return False, '用户不存在'
        if not check_password_hash(row['password_hash'], old_password):
            return False, '原密码不正确'
        conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                     (generate_password_hash(new_password), user_id))
        conn.commit()
        return True, 'ok'
    finally:
        conn.close()


def touch_last_login(base_dir, user_id):
    conn = connect(base_dir)
    try:
        conn.execute('UPDATE users SET last_login=? WHERE id=?', (_now(), user_id))
        conn.commit()
    finally:
        conn.close()


# ---------------- 角色 / 权限 ----------------
def list_roles(base_dir):
    conn = connect(base_dir)
    try:
        rows = conn.execute('SELECT * FROM roles ORDER BY id').fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['user_count'] = conn.execute(
                'SELECT COUNT(*) AS c FROM users WHERE role_id=?', (r['id'],)).fetchone()['c']
            d['permissions'] = get_role_permission_codes(base_dir, r['id'])
            result.append(d)
        return result
    finally:
        conn.close()


def get_role_permission_codes(base_dir, role_id):
    conn = connect(base_dir)
    try:
        rows = conn.execute(
            'SELECT p.code FROM permissions p JOIN role_permissions rp ON p.id=rp.permission_id '
            'WHERE rp.role_id=? ORDER BY p.id', (role_id,)).fetchall()
        return [r['code'] for r in rows]
    finally:
        conn.close()


def set_role_permissions(base_dir, role_id, codes):
    conn = connect(base_dir)
    try:
        conn.execute('DELETE FROM role_permissions WHERE role_id=?', (role_id,))
        for c in codes:
            pid = conn.execute('SELECT id FROM permissions WHERE code=?', (c,)).fetchone()
            if pid:
                conn.execute(
                    'INSERT INTO role_permissions (role_id, permission_id) VALUES (?,?)',
                    (role_id, pid['id']))
        conn.commit()
        return True
    finally:
        conn.close()


def create_role(base_dir, name, code, description=''):
    conn = connect(base_dir)
    try:
        if conn.execute('SELECT id FROM roles WHERE code=?', (code,)).fetchone():
            return None, '角色编码已存在'
        cur = conn.execute(
            'INSERT INTO roles (name, code, description, created_at) VALUES (?,?,?,?)',
            (name, code, description, _now()))
        conn.commit()
        return cur.lastrowid, 'ok'
    finally:
        conn.close()


def update_role(base_dir, role_id, name=None, description=None):
    conn = connect(base_dir)
    try:
        row = conn.execute('SELECT * FROM roles WHERE id=?', (role_id,)).fetchone()
        if not row:
            return False, '角色不存在'
        conn.execute('UPDATE roles SET name=?, description=? WHERE id=?',
                     (name if name is not None else row['name'],
                      description if description is not None else row['description'],
                      role_id))
        conn.commit()
        return True, 'ok'
    finally:
        conn.close()


def delete_role(base_dir, role_id):
    conn = connect(base_dir)
    try:
        row = conn.execute('SELECT * FROM roles WHERE id=?', (role_id,)).fetchone()
        if not row:
            return False, '角色不存在'
        if row['code'] == 'admin':
            return False, '系统管理员角色不可删除'
        used = conn.execute('SELECT COUNT(*) AS c FROM users WHERE role_id=?', (role_id,)).fetchone()['c']
        if used:
            return False, f'该角色下还有 {used} 个用户，无法删除'
        conn.execute('DELETE FROM role_permissions WHERE role_id=?', (role_id,))
        conn.execute('DELETE FROM roles WHERE id=?', (role_id,))
        conn.commit()
        return True, 'ok'
    finally:
        conn.close()


def list_permissions(base_dir):
    conn = connect(base_dir)
    try:
        rows = conn.execute('SELECT * FROM permissions ORDER BY id').fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user_permission_codes(base_dir, user_id):
    conn = connect(base_dir)
    try:
        row = conn.execute('SELECT role_id FROM users WHERE id=?', (user_id,)).fetchone()
        if not row or row['role_id'] is None:
            return []
        return get_role_permission_codes(base_dir, row['role_id'])
    finally:
        conn.close()


# ---------------- 操作日志 ----------------
def add_log(base_dir, username, action, detail='', ip=''):
    try:
        conn = connect(base_dir)
        try:
            conn.execute(
                'INSERT INTO admin_logs (username, action, detail, ip, created_at) VALUES (?,?,?,?,?)',
                (username, action, detail, ip or '', _now()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # 日志失败不影响主流程


def list_logs(base_dir, limit=100, user=None, action=None, keyword=None):
    """查询日志，支持按用户/动作/关键词筛选。"""
    conn = connect(base_dir)
    try:
        sql = 'SELECT * FROM admin_logs WHERE 1=1'
        params = []
        if user:
            sql += ' AND username LIKE ?'
            params.append('%' + user + '%')
        if action:
            sql += ' AND action LIKE ?'
            params.append('%' + action + '%')
        if keyword:
            sql += ' AND (action LIKE ? OR detail LIKE ?)'
            params.append('%' + keyword + '%')
            params.append('%' + keyword + '%')
        sql += ' ORDER BY id DESC LIMIT ?'
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_stats(base_dir, days=7):
    """日志统计：近 N 天每日数量、动作分布、用户排行。"""
    from datetime import timedelta
    conn = connect(base_dir)
    try:
        today = datetime.now().date()
        labels = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days - 1, -1, -1)]
        rows = conn.execute(
            "SELECT substr(created_at,1,10) AS d, COUNT(*) AS c FROM admin_logs GROUP BY d").fetchall()
        cnt = {r['d']: r['c'] for r in rows}
        daily = [{'date': d, 'count': cnt.get(d, 0)} for d in labels]
        by_action = [dict(r) for r in conn.execute(
            'SELECT action, COUNT(*) AS count FROM admin_logs GROUP BY action ORDER BY count DESC').fetchall()]
        by_user = [dict(r) for r in conn.execute(
            'SELECT username, COUNT(*) AS count FROM admin_logs GROUP BY username ORDER BY count DESC LIMIT 10').fetchall()]
        return {'daily': daily, 'by_action': by_action, 'by_user': by_user}
    finally:
        conn.close()

def clear_logs(base_dir):
    conn = connect(base_dir)
    try:
        conn.execute('DELETE FROM admin_logs')
        conn.commit()
    finally:
        conn.close()


def stats(base_dir):
    conn = connect(base_dir)
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        db_file = db_path(base_dir)
        db_size = os.path.getsize(db_file) if os.path.isfile(db_file) else 0
        return {
            'users': conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c'],
            'active_users': conn.execute('SELECT COUNT(*) AS c FROM users WHERE status=1').fetchone()['c'],
            'roles': conn.execute('SELECT COUNT(*) AS c FROM roles').fetchone()['c'],
            'permissions': conn.execute('SELECT COUNT(*) AS c FROM permissions').fetchone()['c'],
            'logs_today': conn.execute(
                "SELECT COUNT(*) AS c FROM admin_logs WHERE created_at LIKE ?", (today + '%',)).fetchone()['c'],
            'total_logs': conn.execute('SELECT COUNT(*) AS c FROM admin_logs').fetchone()['c'],
            'db_size_kb': round(db_size / 1024, 1),
        }
    finally:
        conn.close()

# ---------------- 系统设置（key-value） ----------------
def get_setting(base_dir, key, default=None):
    conn = connect(base_dir)
    try:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default
    finally:
        conn.close()


def set_setting(base_dir, key, value):
    conn = connect(base_dir)
    try:
        conn.execute(
            'INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
            (key, value, _now()))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_setting(base_dir, key):
    conn = connect(base_dir)
    try:
        conn.execute('DELETE FROM settings WHERE key=?', (key,))
        conn.commit()
        return True
    finally:
        conn.close()
