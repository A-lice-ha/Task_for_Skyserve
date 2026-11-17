import xml.etree.ElementTree as ET

import numpy as np

from src.preprocessing.phase2 import apply_radiometry, read_radiometry


def test_reads_l1c_gain_and_per_band_offsets(tmp_path):
    metadata = tmp_path / "MTD_MSIL1C.xml"
    metadata.write_text(
        """<root>
        <QUANTIFICATION_VALUE>10000</QUANTIFICATION_VALUE>
        <RADIO_ADD_OFFSET band_id="1">-1000</RADIO_ADD_OFFSET>
        <RADIO_ADD_OFFSET band_id="2">-900</RADIO_ADD_OFFSET>
        <RADIO_ADD_OFFSET band_id="3">-1000</RADIO_ADD_OFFSET>
        <RADIO_ADD_OFFSET band_id="4">-1000</RADIO_ADD_OFFSET>
        <RADIO_ADD_OFFSET band_id="7">-1000</RADIO_ADD_OFFSET>
        <RADIO_ADD_OFFSET band_id="11">-1000</RADIO_ADD_OFFSET>
        <RADIO_ADD_OFFSET band_id="12">-1000</RADIO_ADD_OFFSET>
        </root>""",
        encoding="utf-8",
    )

    radiometry = read_radiometry(metadata)

    assert radiometry.gain == 10000
    assert radiometry.gain_tag == "QUANTIFICATION_VALUE"
    assert radiometry.offset_tag == "RADIO_ADD_OFFSET"
    assert radiometry.offsets["B03"] == -900


def test_reads_l2a_boa_gain_and_offsets(tmp_path):
    metadata = tmp_path / "MTD_MSIL2A.xml"
    root = ET.Element("root")
    ET.SubElement(root, "BOA_QUANTIFICATION_VALUE").text = "10000"
    for band_id in (1, 2, 3, 7, 11, 12):
        ET.SubElement(
            root, "BOA_ADD_OFFSET", {"band_id": str(band_id)}
        ).text = "-1000"
    ET.ElementTree(root).write(metadata, encoding="utf-8")

    radiometry = read_radiometry(metadata)

    assert radiometry.gain == 10000
    assert radiometry.gain_tag == "BOA_QUANTIFICATION_VALUE"
    assert radiometry.offset_tag == "BOA_ADD_OFFSET"


def test_reflectance_uses_offset_plus_gain():
    actual = apply_radiometry(np.array([0, 1000, 11000], dtype=np.uint16), 10000, -1000)

    np.testing.assert_allclose(actual, [-0.1, 0.0, 1.0])


def test_requires_offset_for_every_selected_band(tmp_path):
    metadata = tmp_path / "MTD_MSIL1C.xml"
    metadata.write_text(
        "<root><QUANTIFICATION_VALUE>10000</QUANTIFICATION_VALUE></root>",
        encoding="utf-8",
    )

    try:
        read_radiometry(metadata)
    except ValueError as error:
        assert "RADIO_ADD_OFFSET" in str(error)
    else:
        raise AssertionError("Missing metadata offsets should be reported")
