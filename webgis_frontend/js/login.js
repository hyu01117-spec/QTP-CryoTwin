/**
 * 登录页脚本：提交用户名/密码到 /api/auth/login，成功后跳转。
 * return 参数仅允许白名单页面，避免开放重定向。
 */
(function () {
  'use strict';

  const form = document.getElementById('login-form');
  const errBox = document.getElementById('login-error');
  const btn = document.getElementById('login-btn');
  const usernameInput = document.getElementById('username');
  const passwordInput = document.getElementById('password');

  const ALLOWED_RETURN = ['index.html', 'query.html', 'simulate.html', 'alert.html', 'decision.html', 'admin.html'];
  const params = new URLSearchParams(window.location.search);
  const ret = params.get('return_url') || params.get('return');
  const returnUrl = ALLOWED_RETURN.includes(ret) ? ret : 'index.html';

  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      errBox.style.display = 'none';
      btn.disabled = true;
      btn.textContent = '登录中...';
      try {
        const resp = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: usernameInput.value.trim(),
            password: passwordInput.value
          })
        });
        const data = await resp.json();
        if (resp.ok && data.success) {
          window.location.href = returnUrl;
        } else {
          errBox.textContent = (data && data.message) || '用户名或密码错误';
          errBox.style.display = 'block';
        }
      } catch (err) {
        errBox.textContent = '网络错误，请稍后重试';
        errBox.style.display = 'block';
      } finally {
        btn.disabled = false;
        btn.textContent = '登 录';
      }
    });
  }
})();