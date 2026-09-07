/* ============================================================
   meteo-query.js —— "气象水文"模块：温度 / 降水 数据查询与上图
   ============================================================
   触发入口：图层管理 → 气象水文 → "温度数据"/"降水数据" checkbox。

   每个 checkbox 勾选后展开二级面板（对应 data/raw/TibetanPlateau/ 下
   两个变量目录）：
     温度数据 → temperature_2m(日均温) / temperature_2m_max(日最高温)
     降水数据 → total_precipitation_sum(日总降水) / total_precipitation_max(日最大降水)

   查询链路：
     前端选 变量 + 起止日期 → GET /api/query/list → 结果表(日期/均值)
     → 点击"加载" → window.renderGeoTiffLayer(file_url, variable, date)

   数据口径：变量与后端 config.VARIABLES 一致；均值单位由后端 /api/query/list
   换算后经 data[].unit 返回，前端仅展示不换算（见设计文档 05 §4.3）。

   依赖：window.UI / window.map / window.renderGeoTiffLayer(geotiff-render.js)
   ============================================================ */
(function () {
  'use strict';

  // 变量元信息：label=中文 radio 文案；html 中用 radio value 存变量 ID
  const DIRS = {
    temperature: {
      checkboxId: 'layer-temperature',
      panelId: 'meteo-temperature-subpanel',
      name: 'meteo-temp-var',
      startId: 'meteo-temp-start',
      endId: 'meteo-temp-end',
      btnId: 'meteo-temp-query',
      tableId: 'meteo-temp-table',
      options: [
        { value: 'temperature_2m', label: '日均温' },
        { value: 'temperature_2m_max', label: '日最高温' }
      ]
    },
    precipitation: {
      checkboxId: 'layer-precipitation',
      panelId: 'meteo-precip-subpanel',
      name: 'meteo-precip-var',
      startId: 'meteo-precip-start',
      endId: 'meteo-precip-end',
      btnId: 'meteo-precip-query',
      tableId: 'meteo-precip-table',
      options: [
        { value: 'total_precipitation_sum', label: '日总降水' },
        { value: 'total_precipitation_max', label: '日最大降水' }
      ]
    }
  };

  // 变量 ID → 展示单位/键（用于结果列；mean 值由后端算好返回）
  const UNIT_BY_VAR = {
    temperature_2m: '℃',
    temperature_2m_max: '℃',
    total_precipitation_sum: 'mm',
    total_precipitation_max: 'mm'
  };

  // 记录每分类当前上图图层（单选展示：再加载一张会替换旧图）
  const activeLayer = { temperature: null, precipitation: null };

  function $(id) { return document.getElementById(id); }
  function toast(msg, type) { if (window.UI && UI.toast) UI.toast(msg, type || 'info'); }

  // ============ 初始化 ============
  function initMeteoQuery() {
    if (window.__meteoQueryInitialized) return;
    window.__meteoQueryInitialized = true;

    Object.keys(DIRS).forEach(dir => bindDir(dir));

    // 变量切换：重查一次（保留旧日期，刷新范围可用性即可）
    Object.keys(DIRS).forEach(dir => {
      const radios = document.querySelectorAll(`input[name="${DIRS[dir].name}"]`);
      radios.forEach(r => r.addEventListener('change', () => refreshRangeHint(dir)));
    });
  }

  function bindDir(dir) {
    const cfg = DIRS[dir];
    const cb = $(cfg.checkboxId);
    const panel = $(cfg.panelId);
    const btn = $(cfg.btnId);
    if (!cb || !panel) return;

    // checkbox 勾选 → 展开/收起面板；收起时移除已上图栅格
    cb.addEventListener('change', () => {
      panel.hidden = !cb.checked;
      if (cb.checked) {
        // 首次展开时拉取该分类默认变量的可用日期范围提示
        refreshRangeHint(dir);
      } else {
        removeActiveLayer(dir);
      }
    });

    if (btn) {
      btn.addEventListener('click', () => runQuery(dir));
    }
  }

  // ============ 范围提示：/api/query/range ============
  async function refreshRangeHint(dir) {
    const cfg = DIRS[dir];
    const varKey = currentVar(dir);
    const startInput = $(cfg.startId);
    const endInput = $(cfg.endId);
    if (!varKey || !startInput || !endInput) return;
    try {
      const res = await fetch(`/api/query/range?variable=${encodeURIComponent(varKey)}`);
      if (!res.ok) return;
      const j = await res.json();
      if (j.status === 'success' && j.available) {
        startInput.min = j.min_date;
        endInput.max = j.max_date;
        if (!startInput.value) startInput.value = j.min_date;
        if (!endInput.value) endInput.value = j.max_date;
      }
    } catch (e) { /* 静默：范围提示失败不影响主查询 */ }
  }

  function currentVar(dir) {
    const sel = document.querySelector(`input[name="${DIRS[dir].name}"]:checked`);
    return sel ? sel.value : null;
  }

  // ============ 主查询：/api/query/list ============
  async function runQuery(dir) {
    const cfg = DIRS[dir];
    const varKey = currentVar(dir);
    const start = $(cfg.startId).value;
    const end = $(cfg.endId).value;
    if (!varKey) { toast('请先选择变量', 'warning'); return; }
    if (!start || !end) { toast('请填写起止日期', 'warning'); return; }
    if (start > end) { toast('开始日期不能晚于结束日期', 'warning'); return; }

    const btn = $(cfg.btnId);
    if (btn) btn.disabled = true;
    if (window.showLoading) window.showLoading(true, '查询中…');
    try {
      const url = `/api/query/list?variable=${encodeURIComponent(varKey)}` +
                  `&start_date=${start}&end_date=${end}&calculate_mean=true`;
      const res = await fetch(url);
      const j = await res.json();
      if (j.status !== 'success') throw new Error(j.message || '查询失败');
      renderResult(dir, varKey, j.data || []);
    } catch (e) {
      console.error('[meteo-query] 查询失败:', e);
      toast('查询失败：' + e.message, 'error');
    } finally {
      if (window.showLoading) window.showLoading(false);
      if (btn) btn.disabled = false;
    }
  }

  // ============ 结果表渲染 ============
  function renderResult(dir, varKey, rows) {
    const cfg = DIRS[dir];
    const tbody = $(cfg.tableId).querySelector('tbody');
    const unit = UNIT_BY_VAR[varKey] || '';
    tbody.innerHTML = '';
    if (!rows.length) {
      toast('该日期范围内无数据', 'warning');
      return;
    }
    rows.forEach(r => {
      const tr = document.createElement('tr');
      const mean = r.mean_value == null ? '—' : Number(r.mean_value).toFixed(2);
      tr.innerHTML = `
        <td>${r.date || ''}</td>
        <td>${mean} ${unit}</td>
        <td><button type="button" class="meteo-load-btn"
             data-var="${varKey}" data-date="${r.date || ''}"
             data-url="${escapeAttr(r.file_path || '')}">加载到地图</button></td>`;
      tbody.appendChild(tr);
    });
    tbody.querySelectorAll('.meteo-load-btn').forEach(b => {
      b.addEventListener('click', () => {
        if (b.disabled) return;
        loadToMap(dir, b.dataset.var, b.dataset.date, b.dataset.url, b);
      });
    });
    toast(`查询到 ${rows.length} 条数据`, 'success');
  }

  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  }

  // ============ 上图：renderGeoTiffLayer ============
  async function loadToMap(dir, varKey, date, relPath, btn) {
    if (!relPath) { toast('缺少文件路径', 'error'); return; }
    const fileUrl = relPath.startsWith('/') ? relPath : `/tif/${relPath}`;
    if (typeof window.renderGeoTiffLayer !== 'function') {
      toast('栅格渲染模块未加载', 'error');
      return;
    }
    btn.disabled = true;
    btn.textContent = '加载中…';
    try {
      // 单选展示：先移除该分类上一次的图层
      removeActiveLayer(dir, true);
      const layer = await window.renderGeoTiffLayer(fileUrl, varKey, date);
      activeLayer[dir] = layer;
      toast('已加载到地图', 'success');
      btn.textContent = '已上图';
    } catch (e) {
      console.error('[meteo-query] 上图失败:', e);
      toast('上图失败：' + e.message, 'error');
      btn.disabled = false;
      btn.textContent = '加载到地图';
    }
  }

  function removeActiveLayer(dir, silent) {
    if (activeLayer[dir]) {
      window.removeGeoTiffLayer && window.removeGeoTiffLayer(activeLayer[dir]);
      activeLayer[dir] = null;
    }
    if (!silent) {
      // 复位该分类下所有"加载"按钮
      const tbody = $(DIRS[dir].tableId);
      if (tbody) tbody.querySelectorAll('.meteo-load-btn').forEach(b => {
        b.disabled = false; b.textContent = '加载到地图';
      });
    }
  }

  // 暴露给页面以便调试 / 其它模块调用
  window.meteoQuery = {
    runQuery, removeActiveLayer
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMeteoQuery);
  } else {
    initMeteoQuery();
  }
})();
