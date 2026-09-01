from nativmix.utils.routing import build_loopback_load_args


def test_build_loopback_load_args_includes_explicit_sink_and_owner_marker():
    assert build_loopback_load_args("NativMix_CH_2.monitor", "alsa_output.usb", 2) == [
        "module-loopback",
        "source=NativMix_CH_2.monitor",
        "sink=alsa_output.usb",
        "sink_input_properties=application.name=NativMixLoopback_CH_2",
        "source_output_properties=application.name=NativMixLoopback_CH_2",
        "source_dont_move=1",
        "sink_dont_move=1",
        "dont-link=1",
    ]
