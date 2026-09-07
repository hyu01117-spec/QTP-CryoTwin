# -*- coding: utf-8 -*-
"""
构建自包含（可重定位）Python 运行时 —— .runtime/python/

背景
----
原 .venv 的 pyvenv.cfg 把母解释器钉死在
    C:\\Users\\<用户名>\\AppData\\Local\\Programs\\Python\\Python312
换一台电脑该路径即失效，虚拟环境整体报废。

本脚本把 CPython 的"内核"复制到项目目录内，再用它装依赖。
CPython 在 Windows 上由 exe 自身位置推导 sys.prefix，
因此整个目录搬到任意盘符 / 任意机器都能直接运行，不依赖目标机是否装过 Python。

用法
----
    python scripts/build_runtime.py            # 提取内核 + 安装依赖
    python scripts/build_runtime.py --core-only  # 只提取内核，不装依赖

内核来源优先级：
    1. 环境变量 PYTHON_SRC 指定的 CPython 安装目录
    2. 自动探测同版本的系统 Python（py -3.12 / where python）
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, '.runtime', 'python')

# CPython 内核：必须有的根目录文件
ROOT_FILES = [
    'python.exe', 'pythonw.exe',
    'python3.dll', 'python312.dll',
    'vcruntime140.dll', 'vcruntime140_1.dll',
    'LICENSE.txt',
]
# 必须整目录复制
COPY_DIRS = ['DLLs', 'include', 'libs', 'tcl']
# Lib 下要排除的（体积大或可再生）
LIB_EXCLUDE = {'site-packages', 'test', '__pycache__', 'idlelib', 'lib2to3'}


def find_source_python():
    """定位同版本（3.12）的系统 CPython 安装目录。"""
    env = os.environ.get('PYTHON_SRC')
    if env and os.path.isfile(os.path.join(env, 'python.exe')):
        return env

    for cmd in (['py', '-3.12', '-c', 'import sys;print(sys.executable)'],):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            p = out.strip()
            if p and os.path.isfile(p):
                return os.path.dirname(p)
        except Exception:
            pass

    import shutil as _s
    for name in ('python3.12', 'python'):
        p = _s.which(name)
        if p:
            try:
                out = subprocess.check_output(
                    [p, '-c', 'import sys;print(sys.version_info[:2])'], text=True)
                if out.strip() == '(3, 12)':
                    return os.path.dirname(p)
            except Exception:
                pass
    return None


def build_core(src):
    """复制 CPython 内核到 .runtime/python/。

    先构建到 .tmp 目录再整体改名就位，避免直接批量删除既有运行时
    （批量删除会触发安全护栏，且中途失败会毁掉可用的旧环境）。
    """
    tmp = RUNTIME + '.tmp'
    old = RUNTIME + '.old'
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    for f in ROOT_FILES:
        s = os.path.join(src, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(tmp, f))

    for d in COPY_DIRS:
        s, d_dst = os.path.join(src, d), os.path.join(tmp, d)
        if os.path.isdir(s):
            shutil.copytree(s, d_dst, dirs_exist_ok=True)

    # Lib：只取标准库，排除 site-packages / test 等
    src_lib, dst_lib = os.path.join(src, 'Lib'), os.path.join(tmp, 'Lib')
    os.makedirs(dst_lib, exist_ok=True)
    for name in os.listdir(src_lib):
        if name in LIB_EXCLUDE:
            continue
        s = os.path.join(src_lib, name)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(dst_lib, name), dirs_exist_ok=True)
        elif os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst_lib, name))

    # 空的 site-packages，供 pip 写入
    os.makedirs(os.path.join(dst_lib, 'site-packages'), exist_ok=True)

    # 旧运行时改名留档（rename 是单步操作，安全）
    if os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(RUNTIME):
        os.rename(RUNTIME, old)
    os.rename(tmp, RUNTIME)
    return os.path.join(RUNTIME, 'python.exe')


# 从 venv 复制时排除的目录（可再生）
# 注意：_distutils_hack 不能排除 —— Python 3.12 已移除标准库 distutils，
# setuptools 靠这个 shim 提供，且 distutils-precedence.pth 会引用它，
# 漏掉会导致每次启动 Python 都打印 ModuleNotFoundError。
SP_EXCLUDE = {'__pycache__'}


def copy_from_venv(venv, dst_sp, py):
    """离线模式：把既有 venv 里装好的包原样搬进运行时。

    适用条件：源 venv 与本运行时是同一个 CPython 构建（版本、架构一致），
    此时二进制扩展模块（.pyd）可直接复用，无需重新下载编译。
    好处是零网络、且版本与当前正在跑的环境完全一致。
    """
    src_sp = os.path.join(venv, 'Lib', 'site-packages')
    if not os.path.isdir(src_sp):
        print('错误：找不到 %s' % src_sp)
        return 1

    # 版本一致性检查：不一致则 .pyd 会加载失败，必须拦住
    vpy = os.path.join(venv, 'Scripts', 'python.exe')
    if os.path.isfile(vpy):
        try:
            out = subprocess.check_output(
                [vpy, '-c', 'import sys;print("%d.%d.%d" % sys.version_info[:3])'],
                text=True).strip()
            mine = subprocess.check_output(
                [py, '-c', 'import sys;print("%d.%d.%d" % sys.version_info[:3])'],
                text=True).strip()
            print('源 venv Python %s / 本运行时 Python %s' % (out, mine))
            if out != mine:
                print('错误：Python 版本不一致，二进制扩展不兼容，无法直接复制。')
                return 1
        except Exception as e:
            print('警告：版本核对失败（%s），继续复制' % e)

    names = [n for n in os.listdir(src_sp) if n not in SP_EXCLUDE]
    print('复制 %d 个条目 → %s' % (len(names), dst_sp))
    ok = err = 0
    for n in names:
        s = os.path.join(src_sp, n)
        d = os.path.join(dst_sp, n)
        try:
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(s, d)
            ok += 1
        except Exception as e:
            print('  跳过 %s: %s' % (n, e))
            err += 1
    print('\n完成：成功 %d，跳过 %d' % (ok, err))
    print('运行时: %s' % RUNTIME)
    return 0 if err == 0 else 1


def run(py, args, cwd=None):
    return subprocess.run([py] + args, cwd=cwd or ROOT, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--core-only', action='store_true',
                    help='只提取 CPython 内核，不装依赖')
    ap.add_argument('--skip-core', action='store_true',
                    help='跳过内核提取，只填充 site-packages')
    ap.add_argument('--from-venv', metavar='PATH',
                    help='离线模式：从既有虚拟环境复制已装好的包（要求 Python 版本完全一致）')
    a = ap.parse_args()

    src = find_source_python()
    if not src and not a.skip_core:
        print('错误：找不到 Python 3.12 安装目录。请设置 PYTHON_SRC 环境变量。')
        return 1
    if src:
        print('内核来源: %s' % src)

    if a.skip_core:
        py = os.path.join(RUNTIME, 'python.exe')
        if not os.path.isfile(py):
            print('错误：--skip-core 但 %s 不存在' % py)
            return 1
    else:
        py = build_core(src)
        print('[1/3] 内核已就位: %s' % py)

        # 自举 pip（stdlib 自带 ensurepip 的 wheel）
        print('[2/3] 自举 pip ...')
        subprocess.run([py, '-m', 'ensurepip', '--upgrade', '--default-pip'],
                       check=True, capture_output=True)
        run(py, ['-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel'])

    if a.core_only:
        print('完成（--core-only，未装项目依赖）')
        return 0

    dst_sp = os.path.join(RUNTIME, 'Lib', 'site-packages')
    os.makedirs(dst_sp, exist_ok=True)

    if a.from_venv:
        return copy_from_venv(a.from_venv, dst_sp, py)

    lock = os.path.join(ROOT, 'requirements.lock.txt')
    if not os.path.isfile(lock):
        print('错误：缺少 requirements.lock.txt')
        return 1
    print('[3/3] 安装锁定依赖（torch 较大，请耐心）...')
    run(py, ['-m', 'pip', 'install', '-r', lock])

    print('\n运行时构建完成: %s' % RUNTIME)
    return 0


if __name__ == '__main__':
    sys.exit(main())
