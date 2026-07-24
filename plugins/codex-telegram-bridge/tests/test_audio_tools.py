import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


AUDIO_TOOLS_PATH = (
    Path(__file__).resolve().parents[1] / "lib" / "audio_tools.py"
)


def load_audio_tools():
    spec = importlib.util.spec_from_file_location(
        "telegram_audio_tools_test",
        AUDIO_TOOLS_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AudioToolsTests(unittest.TestCase):
    def test_working_system_ffmpeg_is_preferred(self):
        audio_tools = load_audio_tools()
        with mock.patch.object(
            audio_tools.shutil,
            "which",
            return_value="/usr/local/bin/ffmpeg",
        ), mock.patch.object(
            audio_tools,
            "_ffmpeg_works",
            return_value=True,
        ):
            executable = audio_tools.resolve_ffmpeg_executable()

        self.assertEqual(executable, "/usr/local/bin/ffmpeg")

    def test_private_converter_is_used_when_system_ffmpeg_is_missing(self):
        audio_tools = load_audio_tools()
        with tempfile.TemporaryDirectory() as tmpdir:
            dependency_dir = Path(tmpdir)
            executable = dependency_dir / "imageio_ffmpeg/binaries/ffmpeg"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"binary")
            package = SimpleNamespace(get_ffmpeg_exe=lambda: str(executable))
            with mock.patch.object(
                audio_tools,
                "DEPENDENCY_DIR",
                dependency_dir,
            ), mock.patch.object(
                audio_tools.shutil,
                "which",
                return_value=None,
            ), mock.patch.object(
                audio_tools.importlib,
                "import_module",
                return_value=package,
            ), mock.patch.object(
                audio_tools,
                "_ffmpeg_works",
                return_value=True,
            ):
                resolved = audio_tools.resolve_ffmpeg_executable()
                ok, detail = audio_tools.ffmpeg_status()

        self.assertEqual(resolved, str(executable))
        self.assertTrue(ok)
        self.assertIn("private setup-managed FFmpeg", detail)

    def test_missing_converter_has_actionable_status(self):
        audio_tools = load_audio_tools()
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            audio_tools,
            "DEPENDENCY_DIR",
            Path(tmpdir) / "missing",
        ), mock.patch.object(
            audio_tools.shutil,
            "which",
            return_value=None,
        ):
            ok, detail = audio_tools.ffmpeg_status()

        self.assertFalse(ok)
        self.assertIn("rerun setup", detail)


if __name__ == "__main__":
    unittest.main()
