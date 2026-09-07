# -*- coding: utf-8 -*-
"""
自动化冒烟测试 —— 核心 API 与知识库可用性验证
=============================================
运行方式（项目根目录）：
    python tests/smoke_test.py

无需外部服务：使用 Flask test client 直接调用应用。
退出码：0 = 全部通过；1 = 存在失败项。
覆盖范围：
  1. 健康检查  /api/health
  2. 专家名录  /api/expert/profiles、/api/expert/fields
  3. 决策内容  /api/decision/content
  4. 灾害库    /api/disaster/types
  5. 图层路由  /api/basin/*（路由注册 + 数据文件存在性）
  6. LLM       /api/llm/health
  7. 认证      /api/auth/login（错误密码 401）
  8. 知识库    expert_kb 构建状态 + RAG 检索（真实数据）
"""
import io
import json
import os
import sys

# 使 webgis_backend 可导入（脚本位于 tests/ 下）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'webgis_backend'))
# Windows 控制台 UTF-8 输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app import create_app  # noqa: E402

app = create_app()
client = app.test_client()

_results = []


def check(name, cond, detail=''):
    _results.append({'name': name, 'ok': bool(cond), 'detail': detail})


# ---------------- 1. 健康检查 ----------------
r = client.get('/api/health')
check('GET /api/health', r.status_code == 200 and r.get_json().get('status') == 'ok',
      'status=%s' % r.status_code)

# ---------------- 2. 专家名录 ----------------
r = client.get('/api/expert/profiles')
d = r.get_json() or {}
check('GET /api/expert/profiles', r.status_code == 200 and d.get('count', 0) >= 7,
      'count=%d' % d.get('count', 0))

r = client.get('/api/expert/fields')
d = r.get_json() or {}
check('GET /api/expert/fields', r.status_code == 200 and len(d.get('fields', [])) >= 5,
      'fields=%d' % len(d.get('fields', [])))

# ---------------- 3. 决策内容 ----------------
r = client.get('/api/decision/content')
d = r.get_json() or {}
c = d.get('counts', {})
check('GET /api/decision/content',
      r.status_code == 200 and c.get('cases', 0) >= 10
      and c.get('engineering', 0) >= 15 and c.get('non_engineering', 0) >= 10,
      'cases=%s eng=%s ne=%s' % (c.get('cases'), c.get('engineering'), c.get('non_engineering')))

# ---------------- 4. 灾害库 ----------------
r = client.get('/api/disaster/types')
d = r.get_json() or {}
check('GET /api/disaster/types', r.status_code == 200 and len(d) >= 8,
      'types=%d' % len(d))

# ---------------- 5. 图层路由与数据文件 ----------------
from modules.basin import basin_bp  # noqa: E402
basin_rules = [str(r) for r in app.url_map.iter_rules() if str(r).startswith('/api/basin/')]
check('图层路由已注册（/api/basin/*）', len(basin_rules) >= 8,
      'routes=%d' % len(basin_rules))
geo_dir = os.path.join(app.config['BASE_DIR'], 'data', 'raw', 'geo')
# geo/ 自 2026-09-07 起按语义分 8 个子目录，需递归统计而非只看根目录
geo_files = []
for _root, _dirs, _files in os.walk(geo_dir):
    geo_files.extend(f for f in _files if f.endswith('.geojson'))
check('GeoJSON 数据目录存在且有文件', len(geo_files) >= 30,
      'files=%d' % len(geo_files))

# ---------------- 6. LLM 健康 ----------------
r = client.get('/api/llm/health')
check('GET /api/llm/health', r.status_code == 200,
      'status=%s' % r.status_code)

# ---------------- 7. 认证（错误密码应 401） ----------------
r = client.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong-password'})
check('POST /api/auth/login 错误密码 -> 401', r.status_code == 401,
      'status=%s' % r.status_code)

# ---------------- 8. 知识库（RAG） ----------------
from modules import expert_kb  # noqa: E402
st = expert_kb.get_kb_status()
counts = st.get('counts', {}) if st else {}
check('知识库已构建', bool(st and st.get('built')),
      'db=%s' % (st or {}).get('db', ''))
check('知识库灾害案例 > 3000', counts.get('kb_disaster_cases', 0) > 3000,
      'cases=%s' % counts.get('kb_disaster_cases'))
check('知识库县域 > 1000', counts.get('kb_counties', 0) > 1000,
      'counties=%s' % counts.get('kb_counties'))

ctx = expert_kb.build_context('雅鲁藏布江流域冰湖溃决洪水风险')
cite_types = [c.get('type', '') for c in ctx.get('citations', [])]
has_glof = any('冰湖' in c.get('summary', '') for c in ctx.get('citations', []))
check('RAG 检索（冰湖溃决）命中灾害案例', len(ctx.get('citations', [])) > 3 and has_glof,
      'citations=%d' % len(ctx.get('citations', [])))

# ---------------- 汇总 ----------------
print('=' * 60)
print('自动化冒烟测试结果')
print('=' * 60)
failed = 0
for res in _results:
    mark = 'PASS' if res['ok'] else 'FAIL'
    if not res['ok']:
        failed += 1
    line = '  [%s] %s' % (mark, res['name'])
    if res['detail']:
        line += '  (%s)' % res['detail']
    print(line)
print('-' * 60)
print('共 %d 项，通过 %d 项，失败 %d 项' % (len(_results), len(_results) - failed, failed))
print('=' * 60)
sys.exit(1 if failed else 0)
