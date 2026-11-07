"""D4-07 演示素材录屏产物收口的回归测试。

覆盖 A 侧复审 P1-2 的「遗留随机文件攻击」：目录里已有上一轮的
``aaa-stale.webm`` 时，finalize_video 必须只认本轮 ``page.video.path()``
的产物，不能把 stale 文件当新产物，且要清理临时产物。
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.demo import capture_demo_screenshots as demo  # noqa: E402


class DemoVideoFinalizeTest(unittest.TestCase):
    def test_finalize_uses_this_run_artifact_not_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stale = d / "aaa-stale.webm"
            fresh = d / "new-recording.webm"
            target = d / "demo_flow.webm"
            stale.write_bytes(b"STALE")
            fresh.write_bytes(b"FRESH_RECORDING")

            result = demo.finalize_video(fresh, target, d)

            self.assertEqual(result, target)
            # 正式文件内容必须来自本轮产物，而非 stale
            self.assertEqual(target.read_bytes(), b"FRESH_RECORDING")
            # stale 与 fresh 临时产物都被清理
            self.assertFalse(stale.exists())
            self.assertFalse(fresh.exists())
            # 目录里只剩 demo_flow.webm
            self.assertEqual([p.name for p in d.glob("*.webm")], ["demo_flow.webm"])

    def test_finalize_rejects_missing_or_empty_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            missing = d / "nope.webm"
            with self.assertRaises(RuntimeError):
                demo.finalize_video(missing, d / "demo_flow.webm", d)
            empty = d / "empty.webm"
            empty.write_bytes(b"")
            with self.assertRaises(RuntimeError):
                demo.finalize_video(empty, d / "demo_flow.webm", d)


class DemoFfmpegPrecheckTest(unittest.TestCase):
    """A 侧第三轮复审 P1：录屏可复现前置需登记 FFmpeg 安装 + 缺失预检。"""

    def _fake_playwright_dir(self, d: Path, with_ffmpeg: bool) -> Path:
        ff = d / "ffmpeg-1000"
        ff.mkdir(parents=True, exist_ok=True)
        if with_ffmpeg:
            (ff / demo._ffmpeg_executable_name()).write_bytes(b"fake")
        return d

    def test_precheck_passes_when_ffmpeg_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = self._fake_playwright_dir(Path(tmp), with_ffmpeg=True)
            with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(d)}):
                demo.precheck_ffmpeg()  # 不抛即通过

    def test_precheck_raises_actionable_error_when_ffmpeg_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)  # 空目录：无 ffmpeg-*
            with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(d)}):
                with self.assertRaises(RuntimeError) as cm:
                    demo.precheck_ffmpeg()
            msg = str(cm.exception)
            # 错误信息必须可操作，给出确切安装命令，而不是 Playwright 深层异常
            self.assertIn("python -m playwright install ffmpeg", msg)
            self.assertIn("ffmpeg", msg)
            self.assertIn(str(d), msg)


if __name__ == "__main__":
    unittest.main()
