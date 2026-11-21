"""Session 15: the optional backdrop dimming that keeps the frame lines legible. Default must leave the approved look byte-identical."""
import dataclasses

from clipengine import render


def test_default_graph_is_unchanged(settings):
    g = render.filter_graph(settings)
    assert "lutyuv" not in g and "eq=brightness=-0.10:saturation=0.85,scale=1080:1920" in g


def test_dim_adds_a_luma_scale_only_to_the_blurred_backdrop(settings):
    g = render.filter_graph(dataclasses.replace(settings, backdrop_dim=0.4))
    assert "eq=brightness=-0.10:saturation=0.85,lutyuv=y=val*0.400,scale=1080:1920" in g
    assert g.count("lutyuv") == 1                          # the central picture is never dimmed


def test_out_of_range_values_are_ignored(settings):
    for v in (0.0, 0.01, 1.0, 1.5):
        assert "lutyuv" not in render.filter_graph(dataclasses.replace(settings, backdrop_dim=v))
