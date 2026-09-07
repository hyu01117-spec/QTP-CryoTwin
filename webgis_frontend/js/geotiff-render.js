/* ============================================================
   geotiff-render.js —— 自包含栅格(TIF)渲染模块（不依赖 simulate.js）
   ============================================================
   目的：为 index.html / query.html 的气象水文查询功能提供"把单日
   温度/降水 TIF 上图"的能力，且不拖入 simulate.js 的整套模拟
   逻辑（simulate.js 是 3400 行单闭包，与其闭包变量深度耦合，不宜在本页复用）。

   依赖（由页面 <script> 按序提供）：
     - window.ol          (js/lib/ol.js)
     - window.GeoTIFF     (js/geotiff.js)
     - window.UI          (js/ui-components.js)  用于 toast（可选降级）
     - window.map         (js/main.js 初始化)

   对外 API：
     - renderGeoTiffLayer(filePath, variable, date, opts?)
         → Promise<ol.layer.Image>  单张上图，并挂一个与图层绑定的图例控件
     - removeGeoTiffLayer(layer)    移除指定栅格图层 + 其图例

   单位说明：TIF 数据本身已是"摄氏度 / 毫米"（实测温度含负值、0 为合法冰点；
   降水 0 = 无降水）。透明规则按变量区分（温度 0/负不透明、降水 0 透明）。
   色带/图例：布局统一对齐 simulate.js 模拟部分（同款图例）；色带颜色源同为
   simulate 那套 20 段彩虹，其中**温度反转**（低温蓝→高温红），降水等用原版。

   说明：simulate.js 内部自有一份近似渲染实现（loadGeoTiffLayer / createLegend），二者
   互不调用、互不影响。为保 simulate.html 零回归，simulate.js 保持不动，故渲染逻辑有
   两份 —— bug 修复需同步。图例布局以本文件为准，与 simulate 视觉一致（色带对温度做反转）。
   ============================================================ */
(function () {
  'use strict';

  // 变量 → 中文名/单位（与 simulate.js i18n.variables/units 保持一致）
  // TIF 数据已是 ℃/mm，此处 unit 仅用作图例标题文字，本模块不做任何单位换算。
  const VAR_META = {
    temperature_2m:          { label: '日均温',    unit: '℃' },
    temperature_2m_max:      { label: '日最高温',  unit: '℃' },
    total_precipitation_sum: { label: '日总降水',  unit: 'mm' },
    total_precipitation_max: { label: '日最大降水', unit: 'mm' },
  };

  // ---- 色带定义（0=低值 → 1=高值）----
  // RAINBOW_STOPS：与 simulate.js 模拟部分一致（径流/融雪/淹没/降水共用），
  //   20 段彩虹：小值偏红 → 大值偏深蓝。
  const RAINBOW_STOPS = [
    { value: 0 / 19,  color: [255, 0, 0] },
    { value: 1 / 19,  color: [255, 64, 0] },
    { value: 2 / 19,  color: [255, 128, 0] },
    { value: 3 / 19,  color: [255, 192, 0] },
    { value: 4 / 19,  color: [255, 255, 0] },
    { value: 5 / 19,  color: [192, 255, 0] },
    { value: 6 / 19,  color: [128, 255, 0] },
    { value: 7 / 19,  color: [64, 255, 0] },
    { value: 8 / 19,  color: [0, 255, 0] },
    { value: 9 / 19,  color: [0, 255, 64] },
    { value: 10 / 19, color: [0, 255, 128] },
    { value: 11 / 19, color: [0, 255, 192] },
    { value: 12 / 19, color: [0, 255, 255] },
    { value: 13 / 19, color: [0, 192, 255] },
    { value: 14 / 19, color: [0, 128, 255] },
    { value: 15 / 19, color: [0, 64, 255] },
    { value: 16 / 19, color: [0, 0, 255] },
    { value: 17 / 19, color: [0, 0, 200] },
    { value: 18 / 19, color: [0, 0, 150] },
    { value: 19 / 19, color: [0, 0, 100] }
  ];

  // 变量 ID → 是否温度类（0 是合法摄氏温度、可出现负值，渲染时不得透明）。
  function isTempVar(variable) {
    return /^temperature_2m|^temperature/i.test(String(variable || ''));
  }

  // 变量 ID → 色带：温度 = simulate 彩虹"反过来"(低值蓝端→高值红端，即低温蓝/高温红)，
  //   其余(降水等) = 原版 simulate 彩虹(小值红→大值深蓝)。颜色源都是同一套 20 段彩虹。
  //   反转需重排 value 为 0→1 递增(interpolateColor 依赖递增分段查找)。
  function colorStopsFor(variable) {
    if (isTempVar(variable)) {
      const rev = RAINBOW_STOPS.slice().reverse();
      return rev.map((s, i) => ({ value: i / (rev.length - 1), color: s.color }));
    }
    return RAINBOW_STOPS;
  }

  // 记录每个图层自带的图例控件，便于统一清理
  const _legendByLayer = new WeakMap();

  function interpolateColor(norm, colorStops) {
    const n = Math.max(0, Math.min(1, norm));
    for (let i = 0; i < colorStops.length - 1; i++) {
      const a = colorStops[i], b = colorStops[i + 1];
      if (n >= a.value && n <= b.value) {
        const t = (n - a.value) / (b.value - a.value);
        return [
          Math.round(a.color[0] + t * (b.color[0] - a.color[0])),
          Math.round(a.color[1] + t * (b.color[1] - a.color[1])),
          Math.round(a.color[2] + t * (b.color[2] - a.color[2]))
        ];
      }
    }
    return n <= 0 ? colorStops[0].color : colorStops[colorStops.length - 1].color;
  }

  // 变量 → 展示名（含单位），fallback 到变量 ID
  function varDisplayName(variable) {
    const key = String(variable || '').toLowerCase();
    const meta = VAR_META[key];
    const label = meta ? meta.label
      : key.includes('inundation') ? '淹没水深'
      : key.includes('flood') ? '洪水'
      : key.includes('snowmelt') ? '融雪量'
      : key.includes('runoff') ? '径流'
      : key.includes('temperature') ? '温度'
      : key.includes('precipitation') ? '降水'
      : key;
    const unit = (meta && meta.unit) || '';
    return unit ? `${label}/${unit}` : label;
  }

  // 数值格式化：与 simulate.js formatValue 一致（<1e-10→'0'，去 .00 尾零，支持负值）
  function formatValue(value, decimalPlaces) {
    if (Math.abs(value) < 1e-10) return '0';
    const formatted = Number(value).toFixed(decimalPlaces);
    if (decimalPlaces > 0 && formatted.endsWith('.00')) return formatted.replace('.00', '');
    return formatted;
  }

  // 建图例控件 —— 布局/配色与 simulate.js createLegend（模拟部分）完全一致：
  //   容器 140px、标题中文名/单位、16 档等分 [min,max]、顶档"大于"、底档"小于"、
  //   中间档 "下 - 上"、色块取每档中点对应的色带色、色块 28×14。
  // 唯一差异：simulate 对 runoff 会把下界 clamp 到 0，本模块保留真实 minValue
  //   （温度含负值、降水 0 透明，故 minValue 即可为真实分位下界）。
  //   decimalPlaces 按档宽自适应且保证非零下界不显示成 0（同 runoff 分支）。
  function createLegendControl(title, minValue, maxValue, colorStops) {
    const stops = colorStops || RAINBOW_STOPS;
    const div = document.createElement('div');
    div.className = 'ol-legend';
    Object.assign(div.style, {
      background: '#1e293b',
      padding: '8px',
      fontSize: '12px',
      border: '1px solid #334155',
      position: 'absolute',
      bottom: '25px',
      left: '150px',
      width: '140px',
      maxWidth: '140px',
      maxHeight: '400px',
      overflow: 'auto',
      boxSizing: 'border-box',
      borderRadius: '8px',
      color: '#e2e8f0',
      boxShadow: '0 2px 8px rgba(0,0,0,0.3)'
    });

    const titleEl = document.createElement('div');
    titleEl.textContent = title;
    Object.assign(titleEl.style, {
      fontWeight: '600', marginBottom: '6px',
      textAlign: 'center', color: '#93c5fd'
    });
    div.appendChild(titleEl);

    const numLegendItems = 16;
    const midCount = numLegendItems - 2;
    const effectiveMinValue = minValue;   // 不做 runoff 的 clamp(0)，保留真实(可负)下界
    const interval = (maxValue - effectiveMinValue) / midCount;
    const span = maxValue - effectiveMinValue;

    // 小数位数：按档宽自适应，且保证非零小下界不显示成 0（对齐 simulate runoff 分支逻辑）
    let decimalPlaces = 2;
    if (span > 0 && interval > 0) {
      decimalPlaces = Math.min(6, Math.max(2, Math.ceil(-Math.log10(interval))));
      // 下界接近 0（如降水 0.003）时提升精度，避免四舍五入成 "小于 0"
      while (decimalPlaces < 6 &&
             parseFloat(effectiveMinValue.toFixed(decimalPlaces)) === 0 &&
             Math.abs(effectiveMinValue) > 0) {
        decimalPlaces++;
      }
    }

    for (let i = 0; i < numLegendItems; i++) {
      const row = document.createElement('div');
      row.style.display = 'flex';
      row.style.alignItems = 'center';
      row.style.marginBottom = '6px';

      const colorBox = document.createElement('div');
      colorBox.style.width = '28px';
      colorBox.style.height = '14px';
      colorBox.style.marginRight = '8px';
      colorBox.style.border = '1px solid #475569';

      let t, label;
      if (i === 0) {
        // 顶档：≥ 98% 分位（渲染 clamp 到色带高值端）
        t = 1;
        label = `大于 ${formatValue(maxValue, decimalPlaces)}`;
      } else if (i === numLegendItems - 1) {
        // 底档：低于 2% 分位的值（0 值不参与配色、图上透明，故用"小于"开区间）
        t = 0;
        label = `小于 ${formatValue(effectiveMinValue, decimalPlaces)}`;
      } else {
        const upperBound = maxValue - (i - 1) * interval;
        const lowerBound = maxValue - i * interval;
        // 色块取该档中点对应的归一化值，保证图例颜色与渲染色带严格一致
        const midPoint = (upperBound + lowerBound) / 2;
        t = span > 0 ? (midPoint - effectiveMinValue) / span : 0.5;
        label = `${formatValue(lowerBound, decimalPlaces)} - ${formatValue(upperBound, decimalPlaces)}`;
      }

      const [r, g, b] = interpolateColor(t, stops);
      colorBox.style.background = `rgb(${r}, ${g}, ${b})`;
      row.appendChild(colorBox);

      const valueText = document.createElement('span');
      valueText.textContent = label;
      row.appendChild(valueText);
      div.appendChild(row);
    }

    return new ol.control.Control({ element: div });
  }

  /**
   * 把单张 GeoTIFF 渲染成地图图层并上图。
   * @param {string} filePath  TIF URL（/tif/... 或 /backend-static/...）
   * @param {string} [variable]  变量 ID（用于图例标题/文件名兜底识别）
   * @param {string} [date]      日期 YYYY-MM-DD（仅透传给图层 title，供调试）
   * @param {Object} [opts]      { showLoading:boolean(默认true), fitView:boolean(默认true) }
   * @returns {Promise<ol.layer.Image>} 成功 resolve 图层；失败抛异常（由调用方提示）
   */
  async function renderGeoTiffLayer(filePath, variable, date, opts) {
    const o = opts || {};
    const map = window.map;
    if (!map) throw new Error('地图未初始化');
    if (!window.GeoTIFF) throw new Error('GeoTIFF 解码库未加载');

    if (o.showLoading !== false && typeof window.showLoading === 'function') {
      window.showLoading(true, '正在加载气象栅格…');
    }
    try {
      const resp = await fetch(filePath);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const buf = await resp.arrayBuffer();
      const tiff = await GeoTIFF.fromArrayBuffer(buf);
      const image = await tiff.getImage();
      const w = image.getWidth(), h = image.getHeight();
      if (!w || !h) throw new Error('无效图像尺寸');

      const bbox = image.getBoundingBox(); // EPSG:4326
      const extentWM = ol.proj.transformExtent(bbox, 'EPSG:4326', 'EPSG:3857');
      const rasters = await image.readRasters({ interleave: true });
      const spp = image.getSamplesPerPixel() || 1;
      const bandData = [];
      for (let i = 0; i < rasters.length; i += spp) bandData.push(rasters[i]);

      // nodata 统一判据：NaN / -9999 / ≤-3e38 哨兵（温度负值如 -30 远大于 -3e38，不会被误判）
      const isNoData = v => v === -9999 || !Number.isFinite(v) || v <= -3e38;
      const validData = bandData.filter(v => !isNoData(v));
      if (!validData.length) throw new Error('TIFF 无有效数据');

      // 温度：0 是合法冰点、可含负值，一律参与配色（仅 nodata 透明）。
      // 降水：0 表示无降水，上图透明、不计入配色分位。
      const temp = isTempVar(variable);
      const colorable = temp ? validData : validData.filter(v => v > 1e-10);
      if (!colorable.length) throw new Error('TIFF 无可着色数据');

      const sorted = Array.from(colorable).sort((a, b) => a - b);
      const pct = p => {
        const idx = (sorted.length - 1) * p;
        const lo = Math.floor(idx), hi = Math.ceil(idx);
        return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
      };
      let minV = pct(0.02), maxV = pct(0.98);
      if (!(maxV > minV)) maxV = minV + Math.max(1e-6, Math.abs(minV) * 0.01);

      // 色带随变量：温度 = 彩虹反转(低温蓝→高温红)，降水等 = simulate 原版彩虹
      const stops = colorStopsFor(variable);

      // 逐像素上色（nodata 透明；降水 0 值也透明）
      const canvas = document.createElement('canvas');
      canvas.width = w; canvas.height = h;
      const ctx = canvas.getContext('2d');
      const imgData = ctx.createImageData(w, h);
      for (let i = 0; i < bandData.length; i++) {
        const v = bandData[i];
        const nodata = isNoData(v);
        const invisible = nodata || (!temp && v <= 1e-10);
        if (invisible) {
          imgData.data[i * 4 + 3] = 0;
        } else {
          const norm = Math.max(0, Math.min(1, (v - minV) / (maxV - minV)));
          const [r, g, b] = interpolateColor(norm, stops);
          imgData.data[i * 4] = r;
          imgData.data[i * 4 + 1] = g;
          imgData.data[i * 4 + 2] = b;
          imgData.data[i * 4 + 3] = 255;
        }
      }
      ctx.putImageData(imgData, 0, 0);

      // 建 ImageCanvas 图层（canvasFunction 负责按视野重采样绘制）
      const layer = new ol.layer.Image({
        source: new ol.source.ImageCanvas({
          canvasFunction: (extent, resolution, pixelRatio, size, projection) => {
            if (projection.getCode() !== 'EPSG:3857') return null;
            const out = document.createElement('canvas');
            out.width = size[0]; out.height = size[1];
            const octx = out.getContext('2d');
            octx.clearRect(0, 0, size[0], size[1]);
            const sX = size[0] / ol.extent.getWidth(extent);
            const sY = size[1] / ol.extent.getHeight(extent);
            const tx = (extentWM[0] - extent[0]) * sX;
            const ty = (extent[3] - extentWM[3]) * sY;
            octx.drawImage(canvas, 0, 0, canvas.width, canvas.height,
              tx, ty, ol.extent.getWidth(extentWM) * sX, ol.extent.getHeight(extentWM) * sY);
            return out;
          },
          projection: 'EPSG:3857',
          imageExtent: extentWM
        }),
        opacity: 0.75,
        visible: true,
        zIndex: 10,
        title: `meteo-raster|${variable}|${date}`
      });
      map.addLayer(layer);

      // 图例绑定到图层（WeakMap 便于移除时统一回收）
      const legend = createLegendControl(varDisplayName(variable), minV, maxV, stops);
      map.addControl(legend);
      _legendByLayer.set(layer, legend);

      if (o.fitView !== false) {
        map.getView().fit(extentWM, { padding: [50, 50, 50, 50], maxZoom: 10 });
      }
      return layer;
    } finally {
      if (o.showLoading !== false && typeof window.showLoading === 'function') {
        window.showLoading(false);
      }
    }
  }

  /** 移除指定栅格图层及其绑定的图例控件。 */
  function removeGeoTiffLayer(layer) {
    if (!layer) return;
    const map = window.map;
    if (!map) return;
    if (map.getLayers().getArray().indexOf(layer) >= 0) map.removeLayer(layer);
    const legend = _legendByLayer.get(layer);
    if (legend && map.getControls().getArray().indexOf(legend) >= 0) {
      map.removeControl(legend);
    }
    _legendByLayer.delete(layer);
  }

  // 暴露
  window.renderGeoTiffLayer = renderGeoTiffLayer;
  window.removeGeoTiffLayer = removeGeoTiffLayer;
})();
