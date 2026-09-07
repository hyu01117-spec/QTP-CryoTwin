# -*- coding: utf-8 -*-
"""
认证模块：登录 / 登出 / 当前用户 / 登录保护装饰器。

- 用户数据存于 admin.db（users 表），密码为哈希存储。
- 首次启动由 admin_db.init_admin_db() 写入管理员账号（账号密码来自 .env）。
- 会话基于 Flask session（cookie），需要 SECRET_KEY。
"""
import functools
from flask import Blueprint, request, jsonify, session, current_app
from werkzeug.security import check_password_hash
from modules import admin_db

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


def _base():
    return current_app.config['BASE_DIR']


def login_required(f):
    """登录保护装饰器：未登录返回 401，前端据此跳转登录页。"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user'):
            return jsonify({'success': False, 'message': '未登录或会话已过期'}), 401
        return f(*args, **kwargs)
    return wrapper


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    user = admin_db.get_user_by_username(_base(), username) if username else None
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401
    if user.get('status') != 1:
        return jsonify({'success': False, 'message': '该账号已被禁用，请联系管理员'}), 403

    session.clear()
    session['user'] = user['username']
    session['user_id'] = user['id']
    session['role_id'] = user['role_id']
    session['role_code'] = user.get('role_code')
    session['display_name'] = user.get('display_name') or user['username']
    session.permanent = True

    admin_db.touch_last_login(_base(), user['id'])
    admin_db.add_log(_base(), user['username'], '登录', '登录系统', request.remote_addr or '')
    return jsonify({
        'success': True,
        'user': user['username'],
        'display_name': session['display_name'],
        'role_code': user.get('role_code'),
    })


@auth_bp.route('/logout', methods=['POST'])
def logout():
    if session.get('user'):
        admin_db.add_log(_base(), session['user'], '退出', '退出系统', request.remote_addr or '')
    session.clear()
    return jsonify({'success': True})


@auth_bp.route('/me', methods=['GET'])
def me():
    user = session.get('user')
    if user:
        return jsonify({
            'success': True,
            'user': user,
            'display_name': session.get('display_name') or user,
            'role_code': session.get('role_code'),
        })
    return jsonify({'success': False, 'message': '未登录'}), 401