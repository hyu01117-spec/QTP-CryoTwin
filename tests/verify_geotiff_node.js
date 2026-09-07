// 用前端实际的 geotiff.js 解码 TIFF，验证后端写出的压缩格式前端能否读取。
// 由 tests/test_tif_frontend_readable.py 调用；也可单独运行：
//   node tests/verify_geotiff_node.js <tif路径> [<参考npy的json路径>]
const fs = require('fs');
const path = require('path');

// geotiff.js 是浏览器包，模块加载时就会引用 Worker / Blob 等浏览器 API。
// 这里补最小桩，让它在 node 下只做同步解码（我们只用到 fromArrayBuffer）。
if (typeof globalThis.Worker === 'undefined') {
  globalThis.Worker = class { constructor() {} postMessage() {} terminate() {} };
}
if (typeof globalThis.Blob === 'undefined') {
  globalThis.Blob = class { constructor(parts) { this.parts = parts; } };
}
if (typeof globalThis.URL.createObjectURL === 'undefined') {
  globalThis.URL.createObjectURL = () => 'data:,';
}

const GeoTIFF = require(path.join(__dirname, '..', 'webgis_frontend', 'js', 'geotiff.js'));

async function main() {
  const file = process.argv[2];
  if (!file) {
    console.log(JSON.stringify({ ok: false, error: '缺少 tif 路径参数' }));
    process.exit(2);
  }
  const buf = fs.readFileSync(file);
  const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  try {
    const tiff = await GeoTIFF.fromArrayBuffer(ab);
    const image = await tiff.getImage();
    const rasters = await image.readRasters();
    const a = rasters[0];
    let nanCount = 0, sum = 0, cnt = 0;
    const step = Math.max(1, Math.floor(a.length / 20000));
    for (let i = 0; i < a.length; i += step) {
      if (Number.isNaN(a[i])) nanCount++;
      else { sum += a[i]; cnt++; }
    }
    console.log(JSON.stringify({
      ok: true,
      length: a.length,
      width: image.getWidth(),
      height: image.getHeight(),
      nanSampled: nanCount,
      nonNanSampled: cnt,
      meanOfSampled: cnt ? sum / cnt : null,
    }));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: e.message }));
    process.exit(1);
  }
}

main();
