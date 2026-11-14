from clipengine import hardware


def test_hardware_selection_rules():
    hw = hardware.HardwareInfo("x", "x", 4, 8, 0, "", 0, None, None)
    assert hardware.choose_whisper(hw) == ("base", "cpu", "int8")
    hw = hardware.HardwareInfo("x", "x", 2, 4, 0, "", 0, None, None)
    assert hardware.choose_whisper(hw, language="en")[0] == "tiny.en"
    hw = hardware.HardwareInfo("x", "x", 16, 32, 1, "RTX", 12, None, None)
    assert hardware.choose_whisper(hw) == ("large-v3", "cuda", "float16")


def test_load_wav_16k(tmp_path):
    import shutil
    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg")
    import subprocess
    from clipengine import media, transcribe
    src = tmp_path / "t.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1", "-ar", "44100", "-ac", "2",
                    str(src)], check=True)
    out = media.extract_audio(src, tmp_path / "o.wav")
    a = transcribe.load_wav_16k(out)
    assert a.dtype.name == "float32" and 15800 < len(a) < 16200 and 0.05 < abs(a).max() <= 1.0
