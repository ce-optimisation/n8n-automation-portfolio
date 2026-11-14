from clipengine import hardware


def test_hardware_selection_rules():
    hw = hardware.HardwareInfo("x", "x", 4, 8, 0, "", 0, None, None)
    assert hardware.choose_whisper(hw) == ("base", "cpu", "int8")
    hw = hardware.HardwareInfo("x", "x", 2, 4, 0, "", 0, None, None)
    assert hardware.choose_whisper(hw, language="en")[0] == "tiny.en"
    hw = hardware.HardwareInfo("x", "x", 16, 32, 1, "RTX", 12, None, None)
    assert hardware.choose_whisper(hw) == ("large-v3", "cuda", "float16")
