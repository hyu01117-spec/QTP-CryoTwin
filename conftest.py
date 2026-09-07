# 统一测试路径注入：让 tests/ 下可直接 `from modules.xxx` / `from webgis_backend...`
# 各测试文件内仍保留各自的 sys.path.insert（兼容旧调用），此处为统一兜底。
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, "webgis_backend")
for p in (BACKEND, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
