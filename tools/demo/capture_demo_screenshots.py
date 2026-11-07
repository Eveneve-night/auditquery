"""D4-07 演示 UI 素材生成：用本机 Edge（Chromium 内核）headless 生成截图 + 录屏。

离线：运行时只访问 127.0.0.1 静态页，不发起模型/网络调用。

可复现前置（依赖已登记在 requirements-dev.txt）：
  - pip install -r requirements-dev.txt
  - 浏览器走 ``channel="msedge"``（本机 Edge），无需 ``playwright install`` 下载 Chromium。
  - 录屏需 Playwright FFmpeg 工具（Python 包本身不携带；这一步是素材生成环境准备，
    会联网下载工具，运行时仍只访问 localhost）：
      python -m playwright install ffmpeg
  - 需先在本机起静态服务：python -m http.server 8899 --directory src/frontend/d4-console

缺失 FFmpeg 时，脚本会在录屏前预检并给出上面的安装命令（而非跑到
``new_context(record_video_dir=...)`` 才抛 Playwright 深层异常）。
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT = PROJECT_ROOT / "deliverables" / "demo-screenshots"
VIDEO_DIR = PROJECT_ROOT / "deliverables" / "demo-video"
BASE_URL = "http://127.0.0.1:8899/index.html"

SCENES = [
    ("01_sql_success", "统计艺术家数量"),
    ("02_sql_malicious_blocked", "删除 Artist 表"),
    ("03_rag_evidence", "月度经营报表的截止时间是什么时候"),
    ("04_rag_insufficient_evidence", "公司健身房几点开门可以洗澡吗"),
    ("05_rag_parse_error", "这个 PDF 解析失败了"),
]

VIDEO_FLOW = [
    "统计艺术家数量",
    "月度经营报表的截止时间是什么时候",
    "这个 PDF 解析失败了",
]


def _playwright_browsers_path() -> Path:
    """返回 Playwright 的浏览器/工具缓存目录（与 Playwright 判定一致）。

    优先 ``PLAYWRIGHT_BROWSERS_PATH``；否则 Windows 用 ``%LOCALAPPDATA%\\ms-playwright``，
    macOS/Linux 用 ``~/.cache/ms-playwright``。
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _ffmpeg_executable_name() -> str:
    """Playwright 各平台 FFmpeg 可执行文件名。

    Windows 为 ``ffmpeg-win64.exe``（本机已核验）；macOS/Linux 遵循 Playwright
    固定命名 ``ffmpeg-mac`` / ``ffmpeg-linux``。
    """
    if os.name == "nt":
        return "ffmpeg-win64.exe"
    if sys.platform == "darwin":
        return "ffmpeg-mac"
    return "ffmpeg-linux"


def precheck_ffmpeg() -> None:
    """预检录屏所需的 Playwright FFmpeg 工具，缺失时给出稳定可操作的错误。

    Playwright 的 Python 包不携带录屏用的 FFmpeg；缺失时会在
    ``browser.new_context(record_video_dir=...)`` 创建页面阶段才抛
    ``Executable doesn't exist at ...ms-playwright/ffmpeg-*/ffmpeg-*.exe``。
    这里提前检查，把「缺工具」与业务失败区分开，避免把 Playwright 深层异常
    误判为演示脚本本身的失败。
    """
    root = _playwright_browsers_path()
    binary = _ffmpeg_executable_name()
    if any((d / binary).is_file() for d in root.glob("ffmpeg-*")):
        return
    raise RuntimeError(
        f"缺少 Playwright 录屏所需的 FFmpeg 工具：未在 {root} 找到 ffmpeg-*/{binary}。\n"
        "请先执行（素材生成环境准备，会联网下载工具；运行时仍只访问 localhost）：\n"
        "  python -m playwright install ffmpeg"
    )


def capture_screenshots(browser) -> int:
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(BASE_URL, wait_until="networkidle")
    page.screenshot(path=str(OUT / "00_initial.png"), full_page=True)
    for name, query in SCENES:
        page.fill("#question-input", query)
        page.click("#ask-submit")
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    page.close()
    return 0


def finalize_video(recorded: Path, target: Path, video_dir: Path) -> Path:
    """把本轮录屏产物原子替换为固定文件，并清理目录里其它 webm 临时产物。

    只认传入的 ``recorded``（来自 ``page.video.path()``，即本轮真实产物），
    不接受「目录里第一个非 demo_flow.webm 的 webm」——那会把上一轮遗留的
    随机文件（如 ``aaa-stale.webm``）当成新产物，导致正式 demo_flow.webm
    内容陈旧、本轮新视频反而残留。
    """
    if not recorded.exists() or recorded.stat().st_size == 0:
        raise RuntimeError(f"录屏产物无效：{recorded}")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(recorded, target)  # 同文件系统内原子替换（覆盖旧 demo_flow.webm）
    for leftover in video_dir.glob("*.webm"):
        if leftover.name != target.name:
            leftover.unlink(missing_ok=True)
    return target


def record_video(browser) -> Path:
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        record_video_dir=str(VIDEO_DIR),
        record_video_size={"width": 1280, "height": 900},
    )
    page = context.new_page()
    recorded: Path | None = None
    try:
        page.goto(BASE_URL, wait_until="networkidle")
        for query in VIDEO_FLOW:
            page.fill("#question-input", query)
            page.click("#ask-submit")
            page.wait_for_timeout(1200)
        # 关闭前精确取得本轮录屏路径（playwright 写入 page@<id>.webm）
        recorded = Path(page.video.path()) if page.video else None
    finally:
        context.close()
    if recorded is None:
        raise RuntimeError("未取得本轮录屏产物（page.video 为空）")
    return finalize_video(recorded, VIDEO_DIR / "demo_flow.webm", VIDEO_DIR)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    precheck_ffmpeg()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        capture_screenshots(browser)
        video = record_video(browser)
        browser.close()

    produced = sorted(OUT.glob("*.png"))
    print(f"共生成 {len(produced)} 张截图：")
    for path in produced:
        print(f"  {path.name}  ({path.stat().st_size} bytes)")
    print(f"录屏：{video.name}  ({video.stat().st_size} bytes)")
    return 0 if produced and video.stat().st_size > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
