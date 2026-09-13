# 品牌图标（桌面快捷方式 / 可复用为 favicon）

- `app-icon.ico` — 当前生效的多尺寸图标（16/24/32/48/64/128/256）
- `app-icon.png` — 512px 主图，可用于文档、汇报封面
- `app-icon-a|b|c.ico` — 三个候选方案：a 低多边形网格雪山（当前默认）、b 雪花等值线、c 风险刻度环
- `make_app_icon.py` — 生成脚本，纯 PIL 绘制，无外部依赖

## 重新生成 / 换方案

```powershell
.\.runtime\python\python.exe assets\branding\make_app_icon.py --outdir assets\branding --default a
```

`--default` 换成 `b` 或 `c` 即可切换；桌面快捷方式需要同步更新图标路径（见下）。

## 桌面快捷方式说明

桌面快捷方式位于 `D:\桌面\青藏高原冰冻圈灾害数字孪生系统.lnk`（桌面被重定向到 `D:\桌面`）。

项目目录名里的连字符是 U+2011（非断行连字符），GBK 里不存在该字符，而 Windows 快捷方式接口
（`WScript.Shell`）会按 ANSI 转换路径，直接写长路径会被写成 `?` 导致失效。因此快捷方式统一使用
8.3 短路径：

- 目标：`%SystemRoot%\System32\cmd.exe`，参数 `/c ""F:\QTPCRY~1\run.bat""`
- 工作目录：`F:\QTPCRY~1`
- 图标：`F:\QTPCRY~1\assets\branding\app-icon.ico`

若项目目录被移动或重命名，短路径失效，需按 `scripts\` 下的方式重建快捷方式。
