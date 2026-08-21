from nativmix.utils.routing import build_loopback_load_args


def test_build_loopback_load_args_includes_explicit_sink():
    assert build_loopback_load_args("NativMix_CH_2.monitor", "alsa_output.usb") == [
        "module-loopback",
        "source=NativMix_CH_2.monitor",
        "sink=alsa_output.usb",
        "dont-link=1",
    ]
