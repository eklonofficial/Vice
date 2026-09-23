import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vice import recorder
from vice.config import Config, OutputConfig, RecordingConfig


class VulkanCodecTests(unittest.TestCase):
    capabilities = frozenset({"h264_software", "h264_vulkan", "hevc_vulkan",
                              "hevc_hdr_vulkan", "hevc_10bit_vulkan"})

    def test_auto_uses_available_vulkan_path(self):
        with mock.patch.object(recorder, "_gsr_supported_codecs", return_value=self.capabilities):
            self.assertEqual(recorder._gsr_codec_args(RecordingConfig(), []), ["-k", "h264_vulkan"])

    def test_explicit_vulkan_codec_and_depth(self):
        for encoder, depth, expected in (
            ("h264_vulkan", "8", "h264_vulkan"),
            ("hevc_vulkan", "8", "hevc_vulkan"),
            ("hevc_vulkan", "10", "hevc_10bit_vulkan"),
            ("h264_vulkan", "10", "hevc_10bit_vulkan"),
            ("av1_vulkan", "10", "av1_10bit_vulkan"),
        ):
            with self.subTest(encoder=encoder, depth=depth):
                self.assertEqual(recorder._gsr_codec_for_encoder(encoder, depth), expected)

    def test_unavailable_traditional_encoder_uses_vulkan_without_losing_depth(self):
        for depth, expected in (("8", "h264_vulkan"), ("10", "hevc_10bit_vulkan")):
            with self.subTest(depth=depth), mock.patch.object(
                    recorder, "_gsr_supported_codecs", return_value=self.capabilities):
                rc = RecordingConfig(encoder="h264_nvenc", color_depth=depth)
                self.assertEqual(recorder._gsr_codec_args(rc, []), ["-k", expected])

    def test_normal_and_unknown_capabilities_preserve_auto(self):
        for capabilities in (frozenset(), frozenset({"h264_software"}),
                             self.capabilities | {"h264"}, self.capabilities | {"hevc"}):
            with self.subTest(capabilities=capabilities), mock.patch.object(
                    recorder, "_gsr_supported_codecs", return_value=capabilities):
                self.assertEqual(recorder._gsr_codec_args(RecordingConfig(), []), [])

    def test_hdr_only_capability_does_not_enable_hdr_for_sdr_recording(self):
        with mock.patch.object(recorder, "_gsr_supported_codecs",
                               return_value=frozenset({"hevc_hdr_vulkan"})):
            self.assertEqual(recorder._gsr_codec_args(RecordingConfig(), []), [])

    def test_overrides_and_cpu_retry_do_not_receive_vulkan_codec(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                recorder, "_gsr_supported_codecs", return_value=self.capabilities):
            rc = RecordingConfig(encoder="hevc_vulkan")
            for extra in (["-k", "av1"], ["-encoder", "cpu"], ["-encoder=cpu"]):
                self.assertEqual(recorder._gsr_codec_args(rc, extra), [])
            instance = recorder.GSRRecorder(Config(recording=rc, output=OutputConfig(directory=directory)))
            self.assertNotIn("-k", instance._build_cmd(cpu_encoder=True))

    def test_replay_and_session_commands_use_the_same_vulkan_codec(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                recorder, "_gsr_supported_codecs", return_value=self.capabilities):
            rc = RecordingConfig(encoder="auto", capture_audio=False)
            instance = recorder.GSRRecorder(Config(recording=rc, output=OutputConfig(directory=directory)))
            for cmd in (instance._build_cmd(), instance._gsr_session_cmd(Path(directory)/"session.mp4", rc)):
                self.assertEqual(cmd[cmd.index("-k") + 1], "h264_vulkan")

    def test_failed_vulkan_codec_has_another_hardware_retry(self):
        with mock.patch.object(recorder, "_gsr_supported_codecs", return_value=self.capabilities):
            self.assertEqual(recorder._gsr_codec_choice(RecordingConfig(), avoid="h264_vulkan"),
                             "hevc_vulkan")

    def test_probe_diagnostics_are_not_codecs(self):
        output = ("section=video_codecs\n"
                  "gsr warning: nvenc API is too old\nh264_software\nh264_vulkan\n"
                  "section=image_formats\njpeg\n")
        recorder._gsr_supported_codecs.cache_clear()
        self.addCleanup(recorder._gsr_supported_codecs.cache_clear)
        with mock.patch.object(recorder, "_has", return_value=True), mock.patch.object(
                recorder, "_run_command_capture", return_value=(0, output)):
            self.assertEqual(recorder._gsr_supported_codecs(), frozenset({"h264_software", "h264_vulkan"}))
