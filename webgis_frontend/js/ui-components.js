/* ============================================================
   UI 组件库 —— 原生 JavaScript 实现，无任何第三方依赖
   提供：Toast 通知 / 自定义弹窗 / 徽章 / 统计卡片 / 环形进度
   全局挂载为 window.UI
   依赖 css/components.css 中的样式
   ============================================================ */
(function () {
  'use strict';

  const UI = {};

  /* ---------------- Toast 通知 ---------------- */
  let toastContainer = null;

  function ensureToastContainer() {
    if (!toastContainer) {
      toastContainer = document.createElement('div');
      toastContainer.className = 'ui-toast-container';
      document.body.appendChild(toastContainer);
    }
    return toastContainer;
  }

  const TOAST_ICONS = {
    success: '✓',
    error: '✕',
    warning: '!',
    info: 'i'
  };

  /**
   * 显示 Toast 通知
   * @param {string} message 提示内容
   * @param {'success'|'error'|'warning'|'info'} [type='info'] 类型
   * @param {number} [duration=3200] 显示时长（ms），传 0 表示不自动关闭
   */
  function toast(message, type = 'info', duration = 3200) {
    const container = ensureToastContainer();
    const el = document.createElement('div');
    el.className = `ui-toast ui-toast-${type}`;
    el.innerHTML = `
      <span class="ui-toast-icon">${TOAST_ICONS[type] || 'i'}</span>
      <span class="ui-toast-message"></span>
      <button class="ui-toast-close" title="关闭">&times;</button>
    `;
    el.querySelector('.ui-toast-message').textContent = message;
    el.querySelector('.ui-toast-close').addEventListener('click', () => dismiss(el));
    container.appendChild(el);

    // 最多同时显示 5 条，超出则移除最旧的
    while (container.children.length > 5) {
      dismiss(container.firstElementChild, 0);
    }

    if (duration > 0) {
      setTimeout(() => dismiss(el), duration);
    }
    return el;
  }

  function dismiss(el, delay = 250) {
    if (!el || el.dataset.leaving) return;
    el.dataset.leaving = '1';
    setTimeout(() => {
      el.classList.add('leaving');
      setTimeout(() => el.remove(), 350);
    }, delay);
  }

  /* ---------------- 自定义弹窗 ---------------- */
  /**
   * 通用弹窗
   * @param {Object} options
   * @param {string} options.title 标题
   * @param {string} options.message 内容
   * @param {string} options.icon 标题图标（Font Awesome class，如 'fa-bell'）
   * @param {Array} options.actions [{text, type, onClick}] 按钮组
   * @param {boolean} options.closeable 是否可点击遮罩关闭，默认 true
   */
  function modal(options) {
    const overlay = document.createElement('div');
    overlay.className = 'ui-modal-overlay';

    const footerActions = (options.actions || []).map(a => {
      const btn = document.createElement('button');
      btn.className = `ui-btn ${a.type || 'ui-btn-ghost'}`;
      btn.textContent = a.text;
      btn.addEventListener('click', () => {
        closeModal();
        if (a.onClick) a.onClick();
      });
      return btn;
    });

    overlay.innerHTML = `
      <div class="ui-modal" role="dialog">
        <div class="ui-modal-header">
          <h3 class="ui-modal-title">
            ${options.icon ? `<i class="fa ${options.icon}"></i>` : ''}
            <span></span>
          </h3>
          <button class="ui-modal-close" title="关闭">&times;</button>
        </div>
        <div class="ui-modal-body"></div>
        <div class="ui-modal-footer"></div>
      </div>
    `;

    overlay.querySelector('.ui-modal-title span').textContent = options.title || '提示';
    overlay.querySelector('.ui-modal-body').textContent = options.message || '';
    const footer = overlay.querySelector('.ui-modal-footer');
    footerActions.forEach(b => footer.appendChild(b));
    if (footerActions.length === 0) footer.remove();

    function closeModal() {
      overlay.classList.remove('active');
      setTimeout(() => overlay.remove(), 280);
      document.removeEventListener('keydown', onKey);
    }

    function onKey(e) {
      if (e.key === 'Escape' && options.closeable !== false) closeModal();
    }

    overlay.querySelector('.ui-modal-close').addEventListener('click', closeModal);
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay && options.closeable !== false) closeModal();
    });
    document.addEventListener('keydown', onKey);

    document.body.appendChild(overlay);
    // 下一帧触发过渡动画
    requestAnimationFrame(() => overlay.classList.add('active'));
    return overlay;
  }

  /**
   * 提示弹窗（替代 alert）
   */
  function alertDialog(options) {
    const opts = typeof options === 'string' ? { message: options } : options;
    return modal({
      title: opts.title || '提示',
      message: opts.message,
      icon: opts.icon || 'fa-info-circle',
      actions: [{ text: opts.confirmText || '知道了', type: 'ui-btn-primary' }]
    });
  }

  /**
   * 确认弹窗（替代 confirm）
   * @returns {Promise<boolean>} resolve(true/false)
   */
  function confirmDialog(options) {
    const opts = typeof options === 'string' ? { message: options } : options;
    return new Promise(resolve => {
      modal({
        title: opts.title || '确认操作',
        message: opts.message,
        icon: opts.icon || 'fa-question-circle',
        closeable: false,
        actions: [
          {
            text: opts.cancelText || '取消',
            type: 'ui-btn-ghost',
            onClick: () => resolve(false)
          },
          {
            text: opts.confirmText || '确定',
            type: opts.danger ? 'ui-btn-danger' : 'ui-btn-primary',
            onClick: () => resolve(true)
          }
        ]
      });
    });
  }

  /* ---------------- 徽章 ---------------- */
  /**
   * 生成徽章 HTML
   * @param {string} text 文本
   * @param {'danger'|'success'|'warning'|'info'|'ice'|'default'} [type='info']
   * @param {Object} [opts] { dot: boolean } 是否显示前置圆点
   */
  function badge(text, type = 'info', opts = {}) {
    return `<span class="ui-badge ui-badge-${type} ${opts.dot === false ? 'pill-none' : ''}">${text}</span>`;
  }

  /* ---------------- 统计卡片 ---------------- */
  /**
   * 生成统计卡片 HTML
   * @param {Object} cfg
   * @param {string} cfg.label 标签
   * @param {string|number} cfg.value 数值
   * @param {string} [cfg.unit] 单位
   * @param {string} [cfg.icon] Font Awesome 图标
   * @param {'danger'|'success'|'warning'|'ice'|'default'} [cfg.accent] 强调色
   */
  function statCard(cfg) {
    const accent = cfg.accent || 'ice';
    return `
      <div class="ui-stat-card" data-accent="${accent}">
        <div class="ui-stat-label">
          ${cfg.icon ? `<i class="fa ${cfg.icon}"></i>` : ''}
          ${cfg.label}
        </div>
        <div class="ui-stat-value">${cfg.value}<span class="ui-stat-unit">${cfg.unit || ''}</span></div>
      </div>
    `;
  }

  /**
   * 生成一组统计卡片（自动网格布局）
   * @param {Array} items 与 statCard 参数一致的对象数组
   */
  function statGrid(items) {
    return `<div class="ui-stat-grid">${items.map(statCard).join('')}</div>`;
  }

  /* ---------------- 环形进度 ---------------- */
  /**
   * 生成环形进度元素
   * @param {number} percent 0~100
   * @param {Object} [opts] { size, color, label, sub }
   * @returns {HTMLElement}
   */
  function ring(percent, opts = {}) {
    const el = document.createElement('div');
    el.className = 'ui-ring';
    const clamped = Math.max(0, Math.min(100, percent));
    el.style.setProperty('--ring-value', clamped);
    el.style.setProperty('--ring-color', opts.color || 'var(--primary)');
    if (opts.size) el.style.setProperty('--ring-size', opts.size + 'px');
    el.innerHTML = `
      <div class="ui-ring-inner">
        ${opts.label !== undefined ? opts.label : Math.round(clamped) + '%'}
        ${opts.sub ? `<small>${opts.sub}</small>` : ''}
      </div>
    `;
    return el;
  }

  /* ---------------- 初始化 ---------------- */
  function init() {
    ensureToastContainer();
  }

  // 暴露全局 API（保留 showAlert 别名，兼容 main.js 等旧代码调用）
  UI.toast = toast;
  UI.modal = modal;
  UI.alert = alertDialog;
  UI.confirm = confirmDialog;
  UI.badge = badge;
  UI.statCard = statCard;
  UI.statGrid = statGrid;
  UI.ring = ring;

  // 兼容旧接口：showAlert(message, type)，type 取值 error/success/info
  UI.showAlert = function (message, type = 'error') {
    const map = { error: 'error', success: 'success', info: 'info', warning: 'warning' };
    return toast(message, map[type] || 'info');
  };


  /* ---------------- 导出工具 ---------------- */
  /**
   * 下载文本文件（CSV / JSON 等）
   * @param {string} filename 文件名
   * @param {string} text 文本内容
   * @param {string} [mime] MIME 类型
   */
  function downloadText(filename, text, mime) {
    const blob = new Blob([text], { type: mime || 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /**
   * 触发浏览器下载一个 URL（同源文件或 dataURL）
   * @param {string} url 文件地址（如 /backend-static/...tif 或 dataURL）
   * @param {string} [filename] 下载文件名
   */
  function downloadUrl(url, filename) {
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || (url.split('/').pop() || 'download');
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  UI.downloadText = downloadText;
  UI.downloadUrl = downloadUrl;

  window.UI = UI;

  // DOM 就绪后初始化
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
