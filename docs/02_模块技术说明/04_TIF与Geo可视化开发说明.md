# TIF 可视化 / Geo 可视化 开发说明（交接版）

> 适用对象：接手本系统栅格（TIF）与矢量（GeoJSON）可视化功能的开发者
> 文档基线：2026-09-07，对应 commit `4c47801`
> 阅读顺序：第 1 章建立全局认知 → 第 2 章理解技术约束 → 第 3、4 章按规范开发 → 第 5、6 章避坑
> **所有结论均附 `文件:行号` 证据，可逐条回溯核对。**

---

## 0. 一分钟速览

| 问题 | 答案 |
|---|---|
| TIF 谁来解析？ | **浏览器端** `webgis_frontend/js/geotiff.js`（geotiff.js v2.x，全局对象 `GeoTIFF`）。服务端不渲染、不转 PNG |
| TIF 怎么上图？ | `GeoTIFF.fromArrayBuffer` → 像素写 `<canvas>` → `ol.source.ImageCanvas` → `ol.layer.Image` |
| 坐标系怎么走？ | 后端写 **EPSG:4326** 的 TIF/GeoJSON → 前端 `ol.proj.transformExtent(..., 'EPSG:4326', 'EPSG:3857')` → 视图 3857 |
| 最硬的约束是什么？ | **TIF 压缩标识必须落在 `{1,5,7,8,32773,34887,50001}`**，禁用 zstd（GDAL 写 50000，前端不认）。见 §2.5 |
| 新增一个栅格图层要改几处？ | 后端 1 个写盘函数 + 1 条静态路由；前端 1 个 `loadXxxLayer`（可复用 `loadGeoTiffLayer`）。模板见 §4.6 |
| 新增一个矢量图层要改几处？ | 后端 1 条 `/api/...` 路由；前端 1 个 `loadXxxData`。模板见 §4.6 |

---

## 1. 功能实现

### 1.1 模块划分

```
┌─ 后端 webgis_backend/ ────────────────────────┐
│ config.py            路径/输出目录/灾害类型常量 │
│ app.py               静态路由、错误处理器       │
│ modules/                                       │
│   runoff_engine.py   ★ 径流推理 + 原子写 TIF   │
│   simulate.py        径流/淹没模拟，写 TIF      │
│   alert.py           风险评估，写分级 TIF       │
│   snowmelt_flood.py  融雪洪水识别，写 TIF       │
│   data_query.py      ERA5 原始 TIF 直出 + 索引  │
│   basin.py           ★ 13 个矢量图层路由        │
│   disaster.py        8 类灾害图层路由           │
│   utils.py           ★ 公共：根目录/JSON缓存/路径防护 │
└───────────────────────────────────────────────┘
                    │ HTTP（/api/*、/backend-static/*、/tif/*）
┌─ 前端 webgis_frontend/ ───────────────────────┐
│ js/geotiff.js     TIF 解码（vendor，v2.x）     │
│ js/main.js        ★ 地图初始化 + 矢量图层加载  │
│ js/simulate.js    ★ TIF 渲染 + 色带 + 时序播放 │
│ js/alert.js       风险分级栅格渲染             │
│ js/query.js       属性查询（不解析 TIF）        │
│ js/ui-components.js  ★ 公共 UI（Toast/Modal）  │
│ css/styles.css    ★ CSS 变量与主题             │
└───────────────────────────────────────────────┘
```

★ = 新增功能时最常打交道的模块。

### 1.2 核心处理流程

**流程 A：TIF 产出（后端）**

```
模拟请求 POST /api/simulate/runoff
  → runoff_engine.simulate_runoff  （runoff_engine.py:345）
      ├─ 按 变量/日期 查索引库 → 得到驱动 TIF 路径（simulate.py:186-210）
      ├─ 裁剪到研究区/流域 → 得到 local_transform（runoff_engine.py:414）
      ├─ 构造 profile：driver=GTiff, dtype=float32, count=1,
      │                compress='lzw', nodata=NaN（runoff_engine.py:419-422）
      └─ write_geotiff_atomic(...)（runoff_engine.py:288）
            ├─ tempfile.mkstemp 写同名目录临时文件
            ├─ os.replace 原子替换
            └─ 失败退避重试 0.15*(attempt+1) 秒，最多 5 次
  → 返回 { urls: ['/backend-static/runoff_results/2024-06-01_runoff.tif', ...] }
```

**流程 B：TIF 渲染（前端）** —— 详见 §2.3

**流程 C：Geo 图层加载（前端）**

```
fetch('/api/basin/data')                       （main.js:1208）
  → new ol.format.GeoJSON().readFeatures(data, { featureProjection:'EPSG:3857' })
  → new ol.source.Vector({ features })
  → new ol.layer.Vector({ source, style, type, zIndex })
  → map.addLayer(layer)                        （main.js:1214-1249）
```

### 1.3 关键数据结构

**① TIF 写出 profile（后端统一）** —— `runoff_engine.py:419-422`

```python
profile.update(driver='GTiff', dtype='float32', count=1,
               compress='lzw', nodata=np.nan,
               height=win_h, width=win_w, transform=local_transform)
```

**② 索引库表结构** —— `data/processed/db/TibetanPlateau_index.db`（活动库，1460 行）

| 表 | 字段 |
|---|---|
| `TibetanPlateau_index` | `id, variable, date, file_path, bbox_minx, bbox_miny, bbox_maxx, bbox_maxy, bands, crs, created_at` |

读取方：`simulate.py:186-210`、`data_query.py:84-92`。
另有 `era5_index.db`（表 `era5_index` 100804 行、`flood_mask_index`、`flood_png_index`），但**后端模拟/查询代码未读取它**，疑似历史遗留 —— 见 §7 待确认。

**③ 前端 TIF 图层对象**

```js
new ol.layer.Image({
  source: new ol.source.ImageCanvas({
    canvasFunction: (extent, resolution, pixelRatio, size, projection) => canvas,
    projection: 'EPSG:3857',
    imageExtent: extentWebMercator     // 由 image.getBoundingBox() 转 3857 得到
  }),
  opacity: 0.75,
  zIndex: 10
})
```

### 1.4 依赖与对外接口

**① TIF 输出静态路由**（均 `send_from_directory`，自带 `..` 拦截）

| 路由 | 物理目录 | 位置 |
|---|---|---|
| `/backend-static/<path:filename>` | `data/webgis/outputs/`（兜底） | `app.py:126-129` |
| `/backend-static/runoff_results/<path:filename>` | `outputs/runoff_results` | `app.py:131-134` |
| `/backend-static/flood_inundation/<path:filename>` | `outputs/flood_inundation` | `app.py:136-140` |
| `/backend-static/risk_results/<path:filename>` | `outputs/risk_results` | `app.py:142-146` |
| `/static/masks\|/pngs\|/animations/<path:filename>` | 同名子目录（**当前均为空**） | `app.py:148-158` |
| `/tif/<path:path>` | `data/raw/TibetanPlateau`（ERA5 原始） | `data_query.py:164` |
| `/api/snowmelt/result/<path:filename>` | `data/processed/GLDAS_snowmelt_max` | `snowmelt_flood.py:348` |

> ⚠️ 注：**不存在** `/webgis_frontend/<path:filename>` 路由。`webgis_frontend` 只是 Flask 的 `static_folder`/`template_folder`（`app.py:38-41`）；`js/`、`css/` 走 `app.py:161-169` 的专用路由。

**② 矢量图层路由**（`url_prefix=/api/basin`，均在 `modules/basin.py`）

| 路由 | 数据源 | 行号 |
|---|---|---|
| `/api/basin/data`、`/search` | `TP_Basins_China.geojson` | `:48`、`:104` |
| `/api/basin/river` | `TP_rivers.geojson` | `:229` |
| `/api/basin/permafrost` | `TP_China_permafrost.geojson` | `:243` |
| `/api/basin/glaciers` | `TP_China_glaciers.geojson` | `:262` |
| `/api/basin/cma` | `TP_Stations.geojson`（气象站，非 CMA） | `:278` |
| `/api/basin/weather_station` | `weather_station.geojson` | `:292` |
| `/api/basin/railway` / `/road` | `rai_4m.geojson` / `roa_4m.geojson` | `:313` / `:334` |
| `/api/basin/settlement` / `/city_settlement` / `/capital_settlement` | `XianCh_point` / `res2_4m` / `res1_4m.geojson` | `:355` / `:375` / `:395` |
| `/api/basin/boundary_country\|province\|city\|county` | `bou1_4m`~`bou4_4m.geojson` | `:415-493` |
| `/api/basin/lake` | `TP_China_Lake.geojson` | `:495` |
| `/api/disaster/<disaster_type>`、`/api/disaster/types` | `config.DISASTER_TYPES` 8 类 | `disaster.py:58`、`:99` |

**③ 直出静态 GeoJSON**：`/data/raw/geo/<path:filename>`（`app.py:173-178`）—— **仅放行 `.geojson` 后缀**，其余 403。

**④ 第三方依赖**

| 依赖 | 版本/来源 | 用途 |
|---|---|---|
| `geotiff.js` | v2.x（UMD，内嵌 Esri LERC 解码器） | 浏览器端 TIF 解码 |
| `OpenLayers`（`js/lib/ol.js`） | 全量打包 | 地图与图层 |
| `ECharts` | — | 折线/统计图表 |
| `rasterio` / `numpy` / `torch` | 见 requirements.txt | 后端栅格读写与推理 |

### 1.5 ★ 可复用的现有方法（优先复用，不要重写）

| 方法 | 位置 | 用途 | 复用建议 |
|---|---|---|---|
| `write_geotiff_atomic(out_path, array, profile, retries=5, logger=None)` | `runoff_engine.py:288` | **原子写 TIF**，规避 Windows 并发删除 `Permission denied` | **任何写盘场景都应该用它**，不要直接 `rasterio.open(p,'w')` |
| `loadGeoTiffLayer(filePath, variable, date)` | `simulate.js:792` | **TIF 渲染通用实现**（解析+拉伸+上色+上图） | 新增栅格图层优先复用；`loadSnowmeltFloodLayer`(:2262)、`loadFloodLayer`(:3213) 都是它的薄封装 |
| `interpolateColor(norm, colorStops)` | `simulate.js:519` | 色带线性插值 | 如需自定义色带，复用此函数只换 `colorStops` |
| `createLegend(variable, colorStops, min, max)` | `simulate.js:548` | 生成图例控件并挂到地图 | 与 `loadGeoTiffLayer` 配套使用 |
| `calculateGeoTiffMean(url)` | `simulate.js:194` | 取 TIF 有效像元均值 | ⚠️ 其 nodata 判据与渲染端不一致，见 §6 |
| `load_json_cached(path)` | `utils.py:13` | 进程内 JSON 缓存（realpath 去重） | 只读 GeoJSON 一律走它，避免每次请求重复解析 |
| `resolve_within(directory, name)` | `utils.py:23` | **路径穿越防护**（纯文件名 + realpath + commonpath） | 新增"按文件名取文件"的路由必须用它 |
| `get_project_root()` | `utils.py:6` | 定位项目根 | 不要自己拼 `../../..` |
| `UI.toast / modal / alert / confirm / badge / ring` | `ui-components.js:37-296` | 统一 UI 组件 | 提示一律用 `UI.toast`，不要裸 `window.alert` |
| `showLoading(bool, text)` | `main.js:2344` | 加载遮罩 | 异步操作前后成对调用 |

---

## 2. 技术路线

### 2.1 技术选型与理由

| 选择 | 理由（来自代码注释与实测） |
|---|---|
| **浏览器端解析 TIF**，而非服务端转 PNG | 服务端零渲染代码（全仓无 `matplotlib` / `rasterio.plot` / `to_geojson` 调用）。保留原始浮点数值，前端可做分位拉伸、均值统计、逐帧动画 |
| **取 `fromArrayBuffer` 而非 `fromUrl`** | `simulate.js:822-825`。先 `fetch` 拿 `arrayBuffer` 便于统一加载遮罩与错误提示 |
| **`ol.source.ImageCanvas` + 自绘 canvas**，而非 `ol/source/GeoTIFF` | 现有 OL 打包版本未含 GeoTIFF 源；且自绘可完全控制 nodata 透明、分位拉伸、分级配色 |
| **输出 `compress='lzw'`**，禁用 zstd | 见 §2.5。**这是踩过线上事故后的硬约束** |
| **原子写 `os.replace`** | `runoff_engine.py:291-305`。`rasterio.open(path,'w')` 内部先删旧文件，Windows 上并发写同一天会 `Permission denied` 直接 500（2026-08-30 线上事故） |
| **后端输出 4326，前端转 3857** | 后端不重投影，保持与源数据一致；显示层由 OL 统一处理 |
| **无构建系统**（原生 JS + script 标签） | 保持可搬运、免安装；代价是无法做静态检查，改动需逐页实测 |

### 2.2 数据流转全景

```
[data/raw/TibetanPlateau/*.tif]  ERA5 驱动栅格（4326）
        │  索引：TibetanPlateau_index.db（variable+date → file_path）
        ▼
[runoff_engine.simulate_runoff]  LSTM 推理
        │  write_geotiff_atomic，compress=lzw，nodata=NaN
        ▼
[data/webgis/outputs/runoff_results/{date}_runoff.tif]
        │  HTTP: /backend-static/runoff_results/...
        ▼
[前端 fetch → arrayBuffer → GeoTIFF.fromArrayBuffer]
        │  image.getBoundingBox() → transformExtent(4326→3857)
        │  readRasters → 2%/98% 分位拉伸 → interpolateColor
        ▼
[<canvas> → ol.source.ImageCanvas → ol.layer.Image]  显示

─────────────────────────────────────────────
[data/raw/geo/*.geojson]（4326 / CRS84）
        │  HTTP: /api/basin/xxx  或  /data/raw/geo/xxx.geojson
        ▼
[ol.format.GeoJSON().readFeatures(data, {featureProjection:'EPSG:3857'})]
        ▼
[ol.layer.Vector + style 函数]  显示
```

### 2.3 TIF 解析与渲染（逐步）

以 `loadGeoTiffLayer`（`simulate.js:792`）为准：

1. **加载与解码**（`:822-825`）
   ```js
   const response = await fetch(filePath);
   const arrayBuffer = await response.arrayBuffer();
   const tiff = await GeoTIFF.fromArrayBuffer(arrayBuffer);
   const image = await tiff.getImage();
   ```
2. **取地理范围**（`:832-833`）
   ```js
   const bbox = image.getBoundingBox();                      // EPSG:4326
   const extentWM = ol.proj.transformExtent(bbox, 'EPSG:4326', 'EPSG:3857');
   ```
3. **读取像素**（`:835`）
   ```js
   const rasters = await image.readRasters({ interleave: true });
   const bandData = rasters[0];   // 约定：单波段
   ```
4. **nodata 判别**（`:844-845`）
   ```js
   const isNoData = v => v === -9999 || !Number.isFinite(v) || v <= -3e38;
   ```
   0 值与 nodata 一律写**透明**（`:905-906`）。
5. **分位拉伸**（`:857-868`）：取有效值的 **2% / 98% 分位** 作为 `minValue/maxValue`（而非 min/max，避免极端值压扁色带）。
6. **归一化与上色**（`:909-911`）
   ```js
   const norm = Math.min(1, Math.max(0, (val - minValue) / (maxValue - minValue)));
   const [r, g, b] = interpolateColor(norm, colorStops);   // simulate.js:519
   ```
7. **建图层**（`:922-958`）：`ol.source.ImageCanvas({canvasFunction, projection:'EPSG:3857', imageExtent: extentWM})` → `ol.layer.Image({opacity:0.75, zIndex:10})`。
8. **视野对齐**（`:979`）：`map.getView().fit(extentWebMercator, {...})`。

**风险分级栅格（alert.js）走的是另一套**：`_renderRiskTif`（`alert.js:468`）不做分位拉伸，而是把值四舍五入到 1~5 级，直接查 `RISK_LEVEL_COLORS`（`alert.js:454`），alpha 固定 220。

### 2.4 性能约束（已实测）

| 约束 | 数值/事实 | 出处 |
|---|---|---|
| 单张径流栅格尺寸 | 4082 × 1560（约 637 万像元） | `test_output_frontend_readable.py:83` |
| 写盘 RMSE 要求 | < 1e-6（float32 无损） | `test_output_frontend_readable.py:136` |
| NaN 掩膜 | 写出再读回必须位置完全一致 | `test_output_frontend_readable.py:137` |
| 原子写重试 | 5 次，退避 `0.15*(attempt+1)` 秒 | `runoff_engine.py:322` 附近 |
| 前端逐像素循环 | 637 万次，主线程同步执行 → 大栅格会卡顿，必须配合加载遮罩 | `simulate.js:895-914` |
| 时序播放 | 链式 `setTimeout`，`loopSpeedMs = 3000 / 倍速`，每帧**重新 fetch + 解码** | `simulate.js:1014-1046`、`:1228` |

> 优化方向（见 §5）：帧缓存、Web Worker 解码、`readRasters({window})` 按需读取。

### 2.5 ⚠️ 兼容性硬约束：TIF 压缩标识

**这是最容易被忽视、也最容易造成线上事故的一条。**

前端 `geotiff.js` 注册的解码器标识集合（`test_output_frontend_readable.py:33`，实测确认）：

```python
FRONTEND_SUPPORTED = {1, 5, 7, 8, 32773, 34887, 50001}
# 1=无压缩  5=LZW  7=JPEG  8=Deflate  32773=PackBits  34887=LERC  50001=ZSTD(带预测器)
```

**GDAL 的 zstd 一律写 TIFF tag 259 = `50000`，该库不认。** 2026-08-30 曾把输出压缩改成 zstd，前端直接抛
`Unknown compression method identifier: 50000`（`test_output_frontend_readable.py:4-7`）。

因此：
- 后端所有输出固定 `compress='lzw'`（`runoff_engine.py:421`、`simulate.py:600`、`alert.py:1182`、`simulate.py:815`）
- **新增任何写盘代码，都不要改这个参数**
- 改动后必须跑 `python tests/test_output_frontend_readable.py`（它会直读 IFD 的 tag 259 并用 node 实跑前端 `geotiff.js` 解码）

---

## 3. 样式要求

### 3.1 界面布局

| 元素 | 约定 | 出处 |
|---|---|---|
| 导航栏高度 | `--nav-height: 120px`（两行） | `styles.css:38` |
| 地图高度 | `--map-height: calc(100vh - var(--nav-height))` | `styles.css:40` |
| 侧边栏宽度 | `--panel-width: 360px` | `styles.css:39` |
| 地图初始视图 | `center: ol.proj.fromLonLat([90, 32])`、`zoom: 8`、`controls: []` | `main.js:108-120` |
| 图例位置 | 由 `ol.control.Control` 挂载（右下角） | `simulate.js:692-693` |
| 弹窗 | `ol.Overlay`，`positioning: 'bottom-center'` | `main.js:472` 等 |

### 3.2 配色

**① 主题变量**（`css/styles.css:5-45`，**必须用变量，不要写死色值**）

| 类别 | 变量 | 值 |
|---|---|---|
| 主色 | `--primary` / `--primary-dark` / `--primary-glow` | `#3b82f6` / `#2563eb` / `rgba(59,130,246,.45)` |
| 冰冻圈 | `--ice` / `--ice-bright` / `--ice-glow` | `#7dd3fc` / `#bae6fd` / `rgba(125,211,252,.35)` |
| 强调 | `--accent-color` | `#67e8f9` |
| 状态 | `--danger` / `--success` / `--warning` | `#f87171` / `#34d399` / `#fbbf24` |
| 背景 | `--bg-color` / `--sidebar-bg` / `--card-bg` / `--card-hover` | `#0a1424` / `#131f33` / `#1b2840` / `#243453` |
| 边框 | `--border-color` / `--sidebar-border` | `#3a4a66` / `#2a3b55` |
| 文字 | `--text-primary` / `--text-secondary` | `#e6edf7` / `#93a4bd` |
| 字体 | `--font-display` / `--font-body` | `'Chakra Petch','Noto Sans SC'` / `'Noto Sans SC','Segoe UI',...` |
| 渐变 | `--grad-logo` / `--grad-topbar` / `--grad-accent-bar` | 见 `styles.css:43-45` |

**② 连续型栅格色带**（`simulate.js:871-892`）—— 20 段彩虹，**径流/融雪/淹没共用**

```
红 [255,0,0] → 橙 → 黄 [255,255,0] → 黄绿 → 绿 [0,255,0]
  → 青绿 → 青 [0,255,255] → 天蓝 → 蓝 [0,0,255] → 深蓝 [0,0,100]
```
小值偏红、大值偏深蓝。`colorStops` 用 `value: n/19` 归一化表达。

**③ 分级型栅格（风险 1~5 级）** —— `alert.js:454` `RISK_LEVEL_COLORS`

| 等级 | RGB | 含义 |
|---|---|---|
| 1 | `[44,123,182]` | 低 |
| 2 | `[171,217,233]` | 较低 |
| 3 | `[255,255,191]` | 中 |
| 4 | `[253,174,97]` | 较高 |
| 5 | `[215,25,28]` | 高 |

**④ 矢量图层配色**（`main.js`）

| 图层 | 样式 | 行号 |
|---|---|---|
| 流域边界 | stroke `#5D4037` w2，fill 空，zIndex 60 | `:1225` |
| 河流 | stroke `#1E90FF` w1.5，zIndex 50 | `:1387-1389` |
| 水文站 | Circle r7 fill `#00f849` stroke `#fff` w2，zIndex 70 | `:1420-1428` |
| 气象站 | Circle r6 fill `#ff6b35` stroke `#fff` w2，zIndex 71 | `:1493-1499` |
| 冰川 | fill `rgba(21,87,230,.7)` stroke `rgba(0,68,255,1)` w1，zIndex 5 | `:2277-2281` |
| 湖泊 | fill `rgba(135,206,235,.7)` stroke `rgba(70,130,180,.9)` w1，zIndex 55 | `:3184-3188` |
| 冻土（按 gridcode） | 0 季节 `rgba(255,255,190,.7)`；1 永久 `rgba(205,205,102,.7)`；2 未冻 `rgba(255,255,255,.7)`；默认 `rgba(255,0,255,.7)` | `:2004-2027` |
| 能源站点 | 水电 `#1E90FF`/风电 `#32CD32`/太阳能 `#FFD700`/火电 `#FF4500`/核电 `#9932CC`，Circle r6 stroke `#fff` | `:347-376` |

**⑤ 灾害事件配色**（`query.js:111-114`）：融雪 `[255,242,0]`、降雨 `[255,140,0]`、冰湖 `[230,0,0]`、堰塞 `[128,0,0]`。

**⑥ 高程色带**（`main.js:2859-2899`）：按 zoom 分级，蓝→绿→黄→橙→红；范围 zoom≥10 用 `(-100~5000)`，≥7 `(-500~8000)`，≥4 `(-1000~10000)`，否则 `(-5000~15000)`。

### 3.3 交互约定

| 交互 | 实现方式 | 出处 |
|---|---|---|
| 点击弹窗 | `ol.Overlay` + `map.on('singleclick')` 命中 feature → `setPosition` | `main.js:472/483` 等 |
| hover | 全局 `pointermove` **仅切换鼠标指针**（`hit ? 'pointer' : ''`），未做要素高亮 | `main.js:2935-2938` |
| 图层开关 | 侧边栏 checkbox `change` → `toggleLayerVisibility(layerId)` | `main.js:3069-3080` |
| 透明度调节 | **未实现**（全仓无 `setOpacity` 调用；TIF 固定 `opacity: 0.75`） | — |
| 时序播放 | `#video-player-controls`（播放/暂停/进度/倍速） | `simulate.js:1065-1162` |
| 地图视野 | 新图层加载后 `map.getView().fit(extent, ...)` | `simulate.js:979` |

### 3.4 状态反馈约定

| 场景 | 标准做法 |
|---|---|
| 加载中 | `showLoading(true, '正在加载…')`（`main.js:2344`）；TIF 渲染也可用 `loadingOverlay.style.display='flex'` |
| 加载完成 | `showLoading(false)`，或 `finally` 中 `display='none'`（`simulate.js:1008`） |
| 成功提示 | `UI.toast('操作成功', 'success')` |
| 业务失败 | `UI.toast(msg, 'error')` |
| 可恢复异常 | `console.warn` + 降级返回（如 `calculateGeoTiffMean` 返回 `null`） |
| 不可恢复异常 | `console.error` + `UI.toast(..., 'error')` |

> ⚠️ 初始化期间（`pageLoadState.init && !finished`）单要素的显隐会被忽略，由加载跟踪器统一收尾（`main.js:2348-2350`）。不要在此期间手动改遮罩。

### 3.5 如何与现有视觉风格保持一致

1. **颜色一律用 CSS 变量**（`var(--primary)` 等），新增语义色先在 `styles.css:5-45` 的变量区声明。
2. **深色底 + 高亮描边**是本项目基调：卡片用 `--card-bg`、边框 `--border-color`、主色点缀 `--primary`/`--ice`。
3. **图例统一 `div.ol-legend` 风格**：深底 `#1e293b`、标题色 `#93c5fd`、色块 28×14px（`simulate.js:566-591`、`:642-689`）。
4. **新增页面组件优先用 `UI.*`**（`ui-components.js`），不要自造弹窗/提示。
5. 按钮用 `ui-btn` 系列：`ui-btn-primary` / `ui-btn-danger` / `ui-btn-ghost` / `ui-btn-sm`。

---

## 4. 格式规则

### 4.1 目录与文件命名

| 类别 | 规则 | 示例 |
|---|---|---|
| Python 模块 | `snake_case`，按业务领域放 `modules/` | `runoff_engine.py` |
| 前端 JS | `snake-case` 或单词，按**页面**划分放 `js/`；vendor 放 `js/lib/` | `simulate.js`、`js/lib/ol.js` |
| 输出 TIF | `{YYYY-MM-DD}_{类型}.tif` | `2024-06-01_runoff.tif` |
| 淹没 TIF | `{YYYY-MM-DD}_flood_inundation.tif` | `simulate.py:587` |
| 风险 TIF | `{start}_to_{end}_basin-{basin}_{risk\|hazard\|exposure\|vulnerability}.tif` | `alert.py:1178-1194` |
| 融雪 TIF | `GLDAS_snowmelt_max_{year}.tif` / `GLDAS_snowmelt_flood_{date}.tif` | `snowmelt_flood.py:41`、`:187` |
| 流域裁剪 | `cropped_{原名}` 或 `..._basin{id}.tif` | `simulate.py:285`、`snowmelt_flood.py:412-415` |
| 矢量数据 | `data/raw/geo/` 下，沿用数据提供方原名 | `TP_Basins_China.geojson` |
| **禁止** | 文件名含空格、`%`、中文（历史遗留除外，新增一律英文 snake_case） | — |

### 4.2 代码风格

**Python**
- 遵循 PEP 8，4 空格缩进，UTF-8 编码头。
- 模块级 `logging.getLogger(__name__)`；业务处用 `current_app.logger`。
- 导入顺序：标准库 → 三方库 → `from config import ...` → `from modules.xxx import ...`。
- ⚠️ **共享常量放 `config.py` 的"模块级"（不是 `Config` 类属性）**，因为部分模块在**导入期**就要读，此时拿不到 `current_app.config`（见 `config.py:12-25` 的 `DISASTER_TYPES` 与其注释）。
- 路径一律用 `get_project_root()` + `os.path.join(...)`，禁止硬编码绝对路径。

**JavaScript**
- 无构建系统，全局函数挂载在 `window` 上跨模块通信（如 `window.map`、`window.tifFileList`）；新增全局变量请在文件顶部集中声明并注释用途。
- 命名：`camelCase` 函数/变量，`PascalCase` 构造函数。
- 异步统一 `async/await` + `try/catch/finally`，`finally` 中收起加载遮罩。

### 4.3 注释与文档规范

**Python docstring 模板**（参考 `runoff_engine.py:288`）

```python
def xxx_atomic(out_path, array, profile, retries=5, logger=None):
    """一句话说明做什么。

    为什么这样做：（背景 / 踩过的坑 / 线上事故编号）
    ...

    已知限制：
    - ...

    Args:
        out_path: ...
    Returns:
        ...
    Raises:
        ...
    """
```

**关键注释必须写「为什么」而不是「做了什么」**。范例（`runoff_engine.py:415-418`）：

```python
# 压缩格式必须限制在前端 webgis_frontend/js/geotiff.js 支持的范围内：
#   1 无压缩 / 5 LZW / 8 Deflate / 32773 PackBits / 34887 LERC / 50001 ZSTD(带预测器)
# GDAL 的 zstd 一律写 tag 50000，该库不认，故禁用。
```

**文档**：技术说明放 `docs/02_模块技术说明/`，评估汇报放 `docs/03_评估与汇报/`，重构方案放 `docs/07_重构方案/`。

### 4.4 配置约定

- **路径常量统一收在 `config.py`**，不要散落在各模块。当前关键常量：

| 常量 | 指向 | 状态 |
|---|---|---|
| `OUTPUTS_DIR` | `data/webgis/outputs` | ✅ |
| `RUNOFF_OUTPUT_DIR` | `outputs/runoff_results` | ✅ |
| `CONTENT_DIR` / `UPLOADS_DIR` | `data/webgis/content` / `data/webgis/uploads` | ✅ |
| `GLDAS_SNOWMELT_MAX_DIR` | `data/processed/GLDAS_snowmelt_max` | ✅ |
| `DATABASE_PATH` | `data/processed/db/TibetanPlateau_index.db` | ✅ |
| `REGION_BOUNDARY_PATH` | `geo/TP_China.geojson`（模拟默认上界） | ✅ |
| `DISASTER_TYPES` | 8 类灾害（文件名+中文名） | ✅ |
| `MODELS_DIR` / `THRESHOLDS_DIR` | `data/raw/models` / `data/raw/thresholds` | ⚠️ 磁盘不存在，依赖外部放入 |
| `DEM_FILE` | `data/raw/dem/wholeYarkant.tif` | ⚠️ 不存在且无人引用，见 §7 |
| `ERA5_DATA_DIR` / `TIF_ROOT_DIR` | `data/raw/TibetanPlateau` | 需核实 |

- **敏感信息**（API Key、密码）一律走环境变量或 `.env`，禁止硬编码（`config.py:4-6` 的安全约定）。

### 4.5 错误处理

**后端**：目前存在**两套并存**的响应格式，新增接口请统一用第一种：

```python
# ✅ 推荐（app.py:194-203 的 errorhandler 即此格式）
return jsonify({'success': False, 'message': '...'}), 400

# ⚠️ 历史遗留（部分 blueprint 在用）
return jsonify({'error': '...'}), 500
```

接口层：`app.py:194-203` 已注册 404/500 处理器，500 **不暴露内部栈**。
模块层：`try/except` + `current_app.logger.error(traceback.format_exc())`。

**前端**：见 §3.4。禁止裸 `window.alert`（`simulate.js:6` 的降级分支仅作兜底）。

### 4.6 ★ 可直接套用的模板

**模板 A：新增一个栅格（TIF）图层**

后端 —— 写盘：

```python
from modules.runoff_engine import write_geotiff_atomic

profile = grid['profile'].copy()
profile.update(driver='GTiff', dtype='float32', count=1,
               compress='lzw',           # ⚠️ 不要改，见 §2.5
               nodata=np.nan,
               height=h, width=w, transform=local_transform)

out_path = os.path.join(current_app.config['XXX_OUTPUT_DIR'],
                        f'{date_str}_xxx.tif')
write_geotiff_atomic(out_path, array, profile, logger=current_app.logger)
return jsonify({'success': True, 'urls': [f'/backend-static/xxx_results/{date_str}_xxx.tif']})
```

后端 —— 静态路由（`app.py`）：

```python
@app.route('/backend-static/xxx_results/<path:filename>')
def serve_xxx_results(filename):
    return send_from_directory(XXX_OUTPUT_DIR, filename)
```

前端 —— 渲染（**直接复用现有实现**）：

```js
await loadGeoTiffLayer('/backend-static/xxx_results/2024-06-01_xxx.tif', 'xxx', '2024-06-01');
```

若要自定义色带，复制 `simulate.js:871-892` 的 `colorStops` 结构，传给 `interpolateColor` / `createLegend`。

---

**模板 B：新增一个矢量（GeoJSON）图层**

后端（`modules/basin.py` 风格）：

```python
@basin_bp.route('/xxx')
def get_xxx_geojson():
    """返回 xxx 图层（EPSG:4326）。"""
    try:
        path = os.path.join(get_project_root(), 'data', 'raw', 'geo', 'xxx.geojson')
        if not os.path.exists(path):
            return jsonify({'success': False, 'message': '数据文件不存在'}), 404
        data = load_json_cached(path)          # 复用缓存，勿用 json.load
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f'加载 xxx 失败: {e}')
        return jsonify({'success': False, 'message': '加载失败'}), 500
```

前端：

```js
async function loadXxxData() {
  showLoading(true, '正在加载 xxx…');
  try {
    const res = await fetch('/api/basin/xxx');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const source = new ol.source.Vector({
      features: new ol.format.GeoJSON().readFeatures(data, {
        featureProjection: 'EPSG:3857'      // 源为 4326，读入即转
      })
    });
    const layer = new ol.layer.Vector({
      source,
      type: 'xxx-layer',
      zIndex: 60,
      style: new ol.style.Style({
        stroke: new ol.style.Stroke({ color: '#5D4037', width: 2 }),
        fill:   new ol.style.Fill({ color: 'rgba(93,64,55,0.15)' })
      })
    });
    map.addLayer(layer);
  } catch (e) {
    console.error('加载 xxx 失败:', e);
    UI.toast('xxx 图层加载失败', 'error');
  } finally {
    showLoading(false);
  }
}
```

---

## 5. 扩展点

| 方向 | 现状 | 建议做法 |
|---|---|---|
| **帧缓存** | 时序播放每帧都重新 fetch + 解码（`simulate.js:1024-1045`） | 缓存已解码的 `{image, rasters}`，来回拖进度条时直接复用 |
| **Web Worker 解码** | 637 万像元在主线程逐像素循环（`simulate.js:895-914`） | 把 `readRasters` + 上色移入 Worker，主线程只接收 canvas |
| **按需读取** | 整幅读入 | `image.readRasters({ window: [...] })` 按视野读窗口 |
| **图层透明度** | 未实现，TIF 固定 `opacity: 0.75` | 复用 `--panel-width` 侧栏加 slider，调 `layer.setOpacity()` |
| **服务端 PNG 预渲染** | `/static/pngs/` 路由存在但无产出逻辑；`flood_png_index` 表有数据 | 可用 `rasterio` + `matplotlib` 预生成缩略图，大范围预览时先用 PNG |
| **矢量要素 hover 高亮** | 只换鼠标指针 | 维护 `select` 交互 + 高亮 style |
| **矢量样式收口** | 颜色散落在 `main.js` 各 `load*` 函数中 | 抽取 `LAYER_STYLES` 常量表（参照 `config.DISASTER_TYPES` 的做法） |

## 6. ⚠️ 常见注意事项（踩坑清单）

1. **压缩参数不能动**。`compress='lzw'` 是踩过线上事故后的硬约束，改前务必读 §2.5 并跑 `tests/test_output_frontend_readable.py`。
2. **写盘必须用 `write_geotiff_atomic`**。直接 `rasterio.open(path,'w')` 会在 Windows 并发时 `Permission denied` → 500。（注：`simulate.py:584-604`、`alert.py:1180-1187` 目前**仍是裸写**，属技术债。）
3. **nodata 判据不一致**：渲染端 `isNoData` 判 `-9999 / 非有限 / ≤-3e38`（`simulate.js:844-845`），而 `calculateGeoTiffMean` 只过滤 `>0`（`simulate.js:211-217`）。均值可能偏差，后续应统一。
4. **`calculateGeoTiffMean` 与渲染端对「0」的处理不同**：渲染把 0 当透明，均值函数把 0 排除。
5. **`data/raw/geo/` 直出路由只放行 `.geojson`**（`app.py:175`），其他后缀一律 403。
6. **`window.disasterLayers` 在 `main.js:3502` 被引用但全仓无赋值** → 灾害图层开关疑似失效，接手时先确认。
7. **`economy_counties.geojson` 文件本身无 CRS 声明**，而 `alert.py:197` 默认按 `EPSG:3857` 处理。若真实是 4326，暴露度插值会整体错位 —— **接手第一件事应核实该文件真实 CRS**。
8. **`geotiff.js` 的 sourcemap（`geotiff.js.map`）不存在**，调试时只能看压缩代码。
9. **`query.html` 引入了 `geotiff.js` 但 `query.js` 从未使用** —— 可移除，也可作为后续栅格功能的接入点。
10. **`simulate.js` 有 59+ 处 `console.log/warn/error`**（`loadGeoTiffLayer` 内尤其密集），上线前应分级或移除。
11. **前端无构建、无类型检查**，任何跨文件改动都必须启动服务逐页实测（7 个页面 + DevTools Console/Network 无 404）。
12. **`MODELS_DIR` / `THRESHOLDS_DIR` 目录在磁盘上不存在**，模拟功能依赖外部放入模型与阈值文件，新环境部署时易漏。

## 7. 待确认事项（接手后请与需求方核对）

| # | 事项 | 影响 |
|---|---|---|
| 1 | `economy_counties.geojson` 的真实 CRS | 风险暴露度计算是否错位 |
| 2 | `era5_index.db` 是否已弃用（后端无代码读取） | 是否可清理 |
| 3 | `config.DEM_FILE` 指向不存在的文件且无人引用，正确路径是否为 `data/processed/dem/web_dem.tif` | 属数据口径，需需求方确认 |
| 4 | `flood_png_index` / `flood_mask_index` 表是否仍有写入方 | `/static/pngs/`、`/static/masks/` 目录为空 |
| 5 | 索引库（`TibetanPlateau_index.db`）由谁生成、何时更新 | 目前后端无 INSERT 代码，疑由 `Training/` 外部脚本生成 |

---

## 附录：改完必跑的验证

```bash
# 1. 压缩兼容性（最关键）
python tests/test_output_frontend_readable.py

# 2. 并发写安全
python tests/test_concurrency_safety.py

# 3. 前端语法
cd webgis_frontend/js && for f in main.js simulate.js alert.js; do node --check $f; done

# 4. 页面与接口实测（用 test_client 或启动服务后逐页点开）
python -c "import sys; sys.path.insert(0,'webgis_backend')
from app import create_app; c=create_app().test_client()
print(c.get('/api/disaster/types').status_code)"
```
