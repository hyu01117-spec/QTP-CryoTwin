/**
 * 决策支持内容渲染（防灾实例 / 工程措施 / 非工程措施）
 * ==========================================
 * 职责：加载 /api/decision/content，渲染 decision.html 中三个侧边栏：
 *   #case-list     防灾实例（真实案例）
 *   #eng-list      工程措施
 *   #ne-list       非工程措施
 * 交互：卡片列表 + 点击展开详情（顶部分类筛选 chips 已移除，卡片保留分类标签），与专家名录风格一致。
 *
 * 安全约定：所有内容以转义文本渲染（XSS 防护）；资料链接新窗口打开。
 */
(function () {
  'use strict';

  const CONFIGS = [
    {
      listEl: 'case-list',
      searchElId: 'case-search-input',
      items: null, filter: 'category',
      cardTitle: 'title', cardSub: 'location',
      detail: function (item) {
        const rows = [];
        rows.push(['事件概况', 'fa-info-circle', item.overview]);
        rows.push(['成因机制', 'fa-cogs', item.mechanism]);
        rows.push(['过程与影响', 'fa-area-chart', item.process]);
        rows.push(['应急处置与经验', 'fa-life-ring', item.response]);
        rows.push(['对本系统研究区的启示', 'fa-lightbulb-o', item.implications]);
        return rows;
      },
      tagField: 'tags', metaField: null
    },
    {
      listEl: 'eng-list',
      searchElId: 'eng-search-input',
      items: null, filter: 'category',
      cardTitle: 'title', cardSub: 'category',
      detail: function (item) {
        const rows = [];
        rows.push(['工程原理', 'fa-wrench', item.principle]);
        rows.push(['适用场景', 'fa-map-marker', item.applicable]);
        if (item.example) rows.push(['工程实例', 'fa-building', item.example]);
        return rows;
      },
      tagField: null, metaField: null
    },
    {
      listEl: 'ne-list',
      searchElId: 'ne-search-input',
      items: null, filter: 'category',
      cardTitle: 'title', cardSub: 'category',
      detail: function (item) {
        const rows = [];
        rows.push(['具体做法', 'fa-check-circle-o', item.practice]);
        rows.push(['关键要点', 'fa-star-o', item.key_points]);
        rows.push(['适用范围', 'fa-map-marker', item.applicable]);
        return rows;
      },
      tagField: null, metaField: null
    }
  ];

  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* 分类 -> 图标（FA 4.7.0 类名，每个分类唯一、语义贴切） */
  const CATEGORY_ICONS = {
    // 防灾实例
    '滑坡堰塞湖': 'fa-arrows-alt',
    '地震次生堰塞湖': 'fa-exclamation-triangle',
    '山洪泥石流': 'fa-tint',
    '冰湖溃决': 'fa-exclamation-circle',
    '冰川泥石流': 'fa-diamond',
    '冰湖防治': 'fa-database',
    '冻土工程': 'fa-thermometer-empty',
    '热融滑塌': 'fa-thermometer-half',
    '冰崩碎屑流': 'fa-bolt',
    // 工程措施
    '防洪工程': 'fa-life-ring',
    '泥石流防治': 'fa-cubes',
    '滑坡与堰塞湖': 'fa-angle-double-down',
    '雪害防治': 'fa-snowflake-o',
    // 非工程措施
    '监测预警': 'fa-bell-o',
    '应急预案': 'fa-file-text-o',
    '群测群防': 'fa-users',
    '宣传教育': 'fa-graduation-cap',
    '风险管理': 'fa-map-o',
    '保险与救助': 'fa-umbrella'
  };

  function iconFor(cat) {
    return CATEGORY_ICONS[cat] || 'fa-info-circle';
  }

  function renderList(cfg, items, filter, emptyMsg) {
    const listEl = document.getElementById(cfg.listEl);
    if (!listEl) return;
    const filtered = filter ? items.filter(function (it) { return it[cfg.filter] === filter; }) : items;
    if (!filtered.length) {
      listEl.innerHTML = '<div class="decision-content-empty">' + escHtml(emptyMsg || '该分类暂无内容') + '</div>';
      return;
    }
    listEl.innerHTML = '';
    filtered.forEach(function (item) {
      const card = document.createElement('div');
      card.className = 'decision-content-card';
      card.dataset.id = item.id;
      const cat = item[cfg.filter] || '';
      const head =
        '<div class="decision-content-card-head">' +
          '<div class="decision-content-card-icon"><i class="fa ' + iconFor(cat) + '"></i></div>' +
          '<div class="decision-content-card-meta">' +
            '<div class="decision-content-card-title">' + escHtml(item[cfg.cardTitle] || '') + '</div>' +
            '<div class="decision-content-card-sub">' +
              (item.year ? '<span class="decision-content-year">' + escHtml(String(item.year)) + '</span>' : '') +
              '<span>' + escHtml(item[cfg.cardSub] || '') + '</span>' +
            '</div>' +
          '</div>' +
          '<div class="decision-content-card-toggle">▸</div>' +
        '</div>';
      const rows = cfg.detail(item);
      let detailHtml = '';
      rows.forEach(function (r) {
        detailHtml += '<div class="decision-detail-block">' +
          '<div class="decision-detail-label">' +
            (r[1] ? '<i class="fa ' + r[1] + '"></i> ' : '') + escHtml(r[0]) + '</div>' +
          '<div>' + escHtml(r[2]) + '</div>' +
        '</div>';
      });
      if (cfg.tagField && item[cfg.tagField] && item[cfg.tagField].length) {
        detailHtml += '<div class="decision-detail-block"><div class="decision-detail-label"><i class="fa fa-tags"></i> 类型标签</div>' +
          '<div class="decision-tags">' + item[cfg.tagField].map(function (t) {
            return '<span class="decision-tag">' + escHtml(t) + '</span>';
          }).join('') + '</div></div>';
      }
      if (item.source) {
        detailHtml += '<div class="decision-detail-block"><div class="decision-detail-label"><i class="fa fa-link"></i> 资料来源</div>' +
          '<div class="decision-source">' + escHtml(item.source) + '</div></div>';
      }
      card.innerHTML = head + '<div class="decision-content-card-detail">' + detailHtml + '</div>';
      card.querySelector('.decision-content-card-head').addEventListener('click', function () {
        const isOpen = card.classList.contains('expanded');
        listEl.querySelectorAll('.decision-content-card.expanded').forEach(function (c) {
          c.classList.remove('expanded');
          c.querySelector('.decision-content-card-toggle').textContent = '▸';
          c.querySelector('.decision-content-card-detail').style.display = 'none';
        });
        if (!isOpen) {
          card.classList.add('expanded');
          card.querySelector('.decision-content-card-toggle').textContent = '▾';
          card.querySelector('.decision-content-card-detail').style.display = 'block';
        }
      });
      listEl.appendChild(card);
    });
  }

  /* 关键词搜索（覆盖 标题/类型/标签/详情文本，与专家名录同款） */
  function matchesQuery(item, q) {
    if (!q) return true;
    var ql = q.toLowerCase();
    for (var k in item) {
      if (!Object.prototype.hasOwnProperty.call(item, k)) continue;
      var v = item[k];
      if (v == null) continue;
      if (Array.isArray(v)) {
        for (var i = 0; i < v.length; i++) {
          if (String(v[i]).toLowerCase().indexOf(ql) !== -1) return true;
        }
      } else if (typeof v === 'string') {
        if (v.toLowerCase().indexOf(ql) !== -1) return true;
      }
    }
    return false;
  }

  function applySearch(cfg) {
    var sEl = document.getElementById(cfg.searchElId);
    var q = sEl ? sEl.value.trim() : '';
    var filtered = cfg.items.filter(function (it) { return matchesQuery(it, q); });
    renderList(cfg, filtered, '', q ? '未找到匹配的内容' : '');
  }

  async function loadContent() {
    try {
      const resp = await fetch('/api/decision/content');
      const data = await resp.json();
      if (!data.success) throw new Error(data.message || '加载失败');
      CONFIGS[0].items = data.cases || [];
      CONFIGS[1].items = data.engineering || [];
      CONFIGS[2].items = data.non_engineering || [];
      CONFIGS.forEach(function (cfg) {
        renderList(cfg, cfg.items, '', '');
        const sEl = document.getElementById(cfg.searchElId);
        if (sEl) sEl.addEventListener('input', function () { applySearch(cfg); });
      });
    } catch (e) {
      console.error('[决策内容] 加载失败:', e);
      ['case-list', 'eng-list', 'ne-list'].forEach(function (id) {
        const el = document.getElementById(id);
        if (el) el.innerHTML = '<div class="decision-content-empty">内容加载失败，请刷新重试</div>';
      });
    }
  }

  loadContent();
})();
