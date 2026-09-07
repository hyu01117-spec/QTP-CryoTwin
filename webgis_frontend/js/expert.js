/**
 * 院士专家名录脚本（原生 JavaScript）
 * ==========================================
 * 职责：绑定 decision.html 专家咨询侧边栏（#expert-search-input / #expert-roster），
 * 实现冰冻圈灾害领域专家名录：
 *   1. 加载专家名录（GET /api/expert/profiles）
 *   2. 关键词搜索（姓名 / 领域 / 单位 / 简介 / 标签）
 *   3. 专家卡片展示多标签（各类冰冻圈灾害 + 研究方向），点击展开详情
 *   4. 展示单位公开联系方式（单位/地址/电话/官网，不展示个人联系方式）
 *
 * 设计说明：
 *   - 不再提供"领域"分类筛选 chips（一位专家往往横跨多个冰冻圈灾害方向，
 *     单分类筛选会遗漏；改为仅保留搜索框，标签本身展示在卡片上并参与搜索）。
 *   - 每位专家的 tags 为多维标签（冰川/积雪/冻土/冰湖溃决/泥石流/… 以及
 *     气候变化/气象/工程防治等方向），搜索框可检索这些标签。
 *
 * 安全约定：所有内容以转义文本渲染（XSS 防护）。
 */
(function () {
  'use strict';

  const searchEl = document.getElementById('expert-search-input');
  const rosterEl = document.getElementById('expert-roster');
  if (!searchEl || !rosterEl) return;

  let allExperts = [];
  let expandedId = null;

  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function avatarColor(name) {
    const colors = ['#1a73e8', '#188038', '#d93025', '#a142f4', '#f29900', '#0d6e6e', '#b35900'];
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return colors[h % colors.length];
  }

  /* ---------------- 数据加载 ---------------- */
  async function loadProfiles() {
    try {
      const resp = await fetch('/api/expert/profiles');
      const data = await resp.json();
      if (!data.success) throw new Error(data.message || '加载失败');
      allExperts = data.experts || [];
      if (!allExperts.length) {
        rosterEl.innerHTML = '<div class="expert-roster-empty">暂未配置专家名录</div>';
        return;
      }
      renderRoster();
    } catch (e) {
      console.error('[专家名录] 加载失败:', e);
      rosterEl.innerHTML = '<div class="expert-roster-empty">专家名录加载失败，请刷新重试</div>';
    }
  }

  /* ---------------- 搜索（含多标签） ---------------- */
  function filteredExperts() {
    const q = searchEl.value.trim().toLowerCase();
    if (!q) return allExperts;
    return allExperts.filter(function (ex) {
      if (q.indexOf((ex.name || '').toLowerCase()) !== -1) return true;
      if (q.indexOf((ex.field || '').toLowerCase()) !== -1) return true;
      if (q.indexOf((ex.institution || '').toLowerCase()) !== -1) return true;
      if (q.indexOf((ex.bio || '').toLowerCase()) !== -1) return true;
      const tags = ex.tags || [];
      for (let i = 0; i < tags.length; i++) {
        if (q.indexOf(String(tags[i]).toLowerCase()) !== -1) return true;
      }
      return false;
    });
  }

  /* ---------------- 标签渲染 ---------------- */
  function renderTags(tags) {
    if (!tags || !tags.length) return '';
    return '<div class="expert-card-tags">' + tags.map(function (t) {
      return '<span class="expert-tag"><i class="fa fa-tag"></i> ' + escHtml(t) + '</span>';
    }).join('') + '</div>';
  }

  /* ---------------- 名录渲染 ---------------- */
  function renderRoster() {
    const list = filteredExperts();
    if (!list.length) {
      rosterEl.innerHTML = '<div class="expert-roster-empty">未找到匹配的专家</div>';
      return;
    }
    rosterEl.innerHTML = '';
    list.forEach(function (ex) {
      const name = ex.name || '';
      const initial = name.charAt(0) || '专';
      const card = document.createElement('div');
      card.className = 'expert-card';
      if (String(ex.id) === String(expandedId)) card.classList.add('expanded');

      const color = ex.avatar_color || avatarColor(name);
      const tagsHtml = renderTags(ex.tags);

      card.innerHTML =
        '<div class="expert-card-head">' +
          '<div class="expert-card-avatar" style="background:' + escHtml(color) + '">' + escHtml(initial) + '</div>' +
          '<div class="expert-card-meta">' +
            '<div class="expert-card-name">' + escHtml(name) + '</div>' +
            '<div class="expert-card-title">' + escHtml(ex.title || '') + '</div>' +
            tagsHtml +
          '</div>' +
          '<div class="expert-card-toggle">' + (String(ex.id) === String(expandedId) ? '▾' : '▸') + '</div>' +
        '</div>' +
        '<div class="expert-card-detail" style="display:' + (String(ex.id) === String(expandedId) ? 'block' : 'none') + ';">' +
          '<div class="expert-detail-block"><div class="expert-detail-label"><i class="fa fa-tag"></i> 研究领域</div><div>' + escHtml(ex.field || '—') + '</div></div>' +
          (tagsHtml ? '<div class="expert-detail-block"><div class="expert-detail-label"><i class="fa fa-tags"></i> 标签</div><div>' + tagsHtml + '</div></div>' : '') +
          '<div class="expert-detail-block"><div class="expert-detail-label"><i class="fa fa-user-o"></i> 学术简介</div><div>' + escHtml(ex.bio || '—') + '</div></div>' +
          '<div class="expert-detail-block"><div class="expert-detail-label"><i class="fa fa-trophy"></i> 代表成果</div><div>' + escHtml(ex.achievements || '—') + '</div></div>' +
          '<div class="expert-detail-block"><div class="expert-detail-label"><i class="fa fa-lightbulb-o"></i> 核心学术观点</div><div>' + escHtml(ex.core_views || '—') + '</div></div>' +
          '<div class="expert-detail-block"><div class="expert-detail-label"><i class="fa fa-file-text-o"></i> 代表论文/著作</div><div>' + escHtml(ex.papers || '—') + '</div></div>' +
          '<div class="expert-detail-block expert-contact-block">' +
            '<div class="expert-detail-label"><i class="fa fa-phone"></i> 联系方式（单位公开渠道）</div>' +
            '<div class="expert-contact-line"><span class="expert-contact-k">单位</span>' + escHtml((ex.contact && ex.contact.unit) || '—') + '</div>' +
            (ex.contact && ex.contact.address ? '<div class="expert-contact-line"><span class="expert-contact-k">地址</span>' + escHtml(ex.contact.address) + '</div>' : '') +
            (ex.contact && ex.contact.phone ? '<div class="expert-contact-line"><span class="expert-contact-k">电话</span>' + escHtml(ex.contact.phone) + '</div>' : '') +
            (ex.contact && ex.contact.website ? '<div class="expert-contact-line"><span class="expert-contact-k">官网</span><a href="' + escHtml(ex.contact.website) + '" target="_blank" rel="noopener">' + escHtml(ex.contact.website) + ' <i class="fa fa-external-link"></i></a></div>' : '') +
            '<div class="expert-contact-note"><i class="fa fa-info-circle"></i> 请通过专家所在单位联系，不展示个人联系方式。</div>' +
          '</div>' +
          ((ex.sources && ex.sources.length) ? '<div class="expert-detail-block"><div class="expert-detail-label"><i class="fa fa-link"></i> 信息来源</div><div class="expert-sources">' +
            ex.sources.map(function (s) { return '<span class="expert-source">' + escHtml(s) + '</span>'; }).join('') +
          '</div></div>' : '') +
        '</div>';

      card.querySelector('.expert-card-head').addEventListener('click', function () {
        expandedId = (String(ex.id) === String(expandedId)) ? null : ex.id;
        renderRoster();
      });
      rosterEl.appendChild(card);
    });
  }

  /* ---------------- 事件绑定 ---------------- */
  searchEl.addEventListener('input', function () {
    renderRoster();
  });

  // 初始化
  loadProfiles();
})();
