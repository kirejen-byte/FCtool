from motd_builder import fitting_link, char_link, channel_text, estimate_length, MOTD_BUDGET_DEFAULT


def test_fitting_link_is_self_contained_dna():
    link = fitting_link("12015:2185;5::", "Arty Muninn")
    assert link == "<url=fitting:12015:2185;5::>Arty Muninn</url>"


def test_char_link_uses_showinfo():
    assert char_link(90000001, "Securitas Protector") == \
        "<url=showinfo:1377//90000001>Securitas Protector</url>"


def test_channel_text_plain_without_id_clickable_with_id():
    assert channel_text("Cap Chain Alpha") == "Cap Chain Alpha"
    assert channel_text("Cap Chain Alpha", channel_id=-99) == \
        "<url=joinChannel:-99>Cap Chain Alpha</url>"


def test_estimate_length_counts_raw_markup():
    s = "<b>x</b>"
    assert estimate_length(s) == len(s)
    assert MOTD_BUDGET_DEFAULT == 3000


from motd_builder import build_motd


def test_build_motd_assembles_fc_doctrine_fits_channel():
    motd = build_motd(
        fc_name="Securitas Protector", fc_character_id=90000001,
        doctrine_name="Shield HACs",
        fits_by_tag={"DPS": [("12015:2185;5::", "Arty Muninn")],
                     "Logistics": [("11985::", "Shield Scimitar")]},
        channel="Cap Chain Alpha", header="Form up Jita", footer="x in fleet")
    assert "Form up Jita" in motd
    assert "<url=showinfo:1377//90000001>Securitas Protector</url>" in motd
    assert "Shield HACs" in motd                       # doctrine name is plain text
    assert "<url=fitting:12015:2185;5::>Arty Muninn</url>" in motd
    assert "Cap Chain Alpha" in motd
    assert "x in fleet" in motd


def test_build_motd_blank_fc_omits_fc_line():
    motd = build_motd(fc_name=None, fc_character_id=None, doctrine_name="D",
                      fits_by_tag={"DPS": [("670::", "Pod")]})
    assert "showinfo" not in motd


from motd_builder import parse_motd


def test_parse_motd_extracts_fc_and_fits():
    raw = ("Form up<br><url=showinfo:1377//90000001>Securitas Protector</url><br>"
           "<url=fitting:12015:2185;5::>Arty Muninn</url> "
           "<url=fitting:11985::>Shield Scimitar</url>")
    out = parse_motd(raw)
    assert out["fc"]["character_id"] == 90000001
    assert out["fc"]["name"] == "Securitas Protector"
    dnas = {f["dna"] for f in out["fittings"]}
    assert dnas == {"12015:2185;5::", "11985::"}
    assert out["raw"] == raw


def test_parse_motd_no_links_is_empty():
    out = parse_motd("just text")
    assert out["fc"] is None and out["fittings"] == []
