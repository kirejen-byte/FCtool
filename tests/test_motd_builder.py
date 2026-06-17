from motd_builder import (
    fitting_link, char_link, channel_text, system_link, estimate_length,
    MOTD_BUDGET_DEFAULT,
)


def test_fitting_link_is_self_contained_dna():
    link = fitting_link("12015:2185;5::", "Arty Muninn")
    assert link == "<url=fitting:12015:2185;5::>Arty Muninn</url>"


def test_system_link_uses_showinfo_type_5():
    assert system_link(30000142, "Jita") == \
        "<url=showinfo:5//30000142>Jita</url>"


def test_char_link_uses_showinfo():
    assert char_link(90000001, "Securitas Protector") == \
        "<url=showinfo:1377//90000001>Securitas Protector</url>"


def test_channel_text_plain_without_id_clickable_with_id():
    assert channel_text("Cap Chain Alpha") == "Cap Chain Alpha"
    # Player channels (negative id) now use the COMPOUND joinChannel form.
    assert channel_text("Cap Chain Alpha", channel_id=-99) == \
        "<url=joinChannel:player_-99//None//None>Cap Chain Alpha</url>"


def test_channel_text_compound_player_id_from_string():
    # A negative-id string is a player channel → compound form, core kept raw.
    assert channel_text("Cap Chain Alpha", channel_id="-84651075") == \
        "<url=joinChannel:player_-84651075//None//None>Cap Chain Alpha</url>"


def test_channel_text_already_player_prefixed_not_double_prefixed():
    # "player_"-prefixed ids must not be double-prefixed.
    assert channel_text("Services", channel_id="player_-84651075") == \
        "<url=joinChannel:player_-84651075//None//None>Services</url>"


def test_channel_text_positive_builtin_id_is_bare():
    # Built-in channels use a bare positive id, no compound wrapper.
    assert channel_text("Help", channel_id=2) == \
        "<url=joinChannel:2>Help</url>"
    assert channel_text("Help", channel_id="2") == \
        "<url=joinChannel:2>Help</url>"


def test_channel_text_empty_or_none_is_plain():
    assert channel_text("Cap Chain Alpha", channel_id=None) == "Cap Chain Alpha"
    assert channel_text("Cap Chain Alpha", channel_id="") == "Cap Chain Alpha"


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


def test_build_motd_staging_line_after_fc_before_doctrine():
    motd = build_motd(
        fc_name="Securitas Protector", fc_character_id=90000001,
        doctrine_name="Shield HACs",
        fits_by_tag={"DPS": [("12015:2185;5::", "Arty Muninn")]},
        staging_name="Jita", staging_system_id=30000142)
    assert "Staging: <url=showinfo:5//30000142>Jita</url>" in motd
    # Staging sits between the FC line and the Doctrine line.
    fc_idx = motd.index("FC: ")
    staging_idx = motd.index("Staging: ")
    doctrine_idx = motd.index("Doctrine: ")
    assert fc_idx < staging_idx < doctrine_idx


def test_build_motd_no_staging_when_absent():
    motd = build_motd(
        fc_name=None, fc_character_id=None, doctrine_name="D",
        fits_by_tag={"DPS": [("670::", "Pod")]})
    assert "Staging:" not in motd


def test_build_motd_staging_omitted_when_only_one_arg():
    # Both name and id are required; one alone is ignored entirely.
    motd_name_only = build_motd(
        fc_name=None, fc_character_id=None, doctrine_name="D",
        fits_by_tag={"DPS": [("670::", "Pod")]}, staging_name="Jita")
    motd_id_only = build_motd(
        fc_name=None, fc_character_id=None, doctrine_name="D",
        fits_by_tag={"DPS": [("670::", "Pod")]}, staging_system_id=30000142)
    assert "Staging:" not in motd_name_only
    assert "Staging:" not in motd_id_only


def test_build_motd_wraps_body_in_white_color_by_default():
    motd = build_motd(
        fc_name="Securitas Protector", fc_character_id=90000001,
        doctrine_name="Shield HACs",
        fits_by_tag={"DPS": [("12015:2185;5::", "Arty Muninn")]})
    # White wrapper present by default (in-game default text renders red).
    assert motd.startswith("<color=0xffffffff>")
    assert motd.endswith("</color>")
    # Inner content survives inside the wrapper.
    assert "<url=showinfo:1377//90000001>Securitas Protector</url>" in motd
    assert "<url=fitting:12015:2185;5::>Arty Muninn</url>" in motd


def test_build_motd_no_color_wrapper_when_text_color_none():
    motd = build_motd(
        fc_name="Securitas Protector", fc_character_id=90000001,
        doctrine_name="Shield HACs",
        fits_by_tag={"DPS": [("12015:2185;5::", "Arty Muninn")]},
        text_color=None)
    assert "<color=" not in motd
    assert not motd.startswith("<color=")
    # Inner content still present, just unwrapped.
    assert "<url=showinfo:1377//90000001>Securitas Protector</url>" in motd
    assert "<url=fitting:12015:2185;5::>Arty Muninn</url>" in motd


def test_build_motd_channel_id_makes_logi_clickable_compound():
    motd = build_motd(
        fc_name=None, fc_character_id=None, doctrine_name="D",
        fits_by_tag={"DPS": [("670::", "Pod")]},
        channel="Cap Chain Alpha", channel_id="-84651075")
    assert ("<url=joinChannel:player_-84651075//None//None>"
            "Cap Chain Alpha</url>") in motd


def test_build_motd_channel_without_id_stays_plain():
    motd = build_motd(
        fc_name=None, fc_character_id=None, doctrine_name="D",
        fits_by_tag={"DPS": [("670::", "Pod")]},
        channel="Cap Chain Alpha")
    assert "joinChannel" not in motd
    assert "Logi: Cap Chain Alpha" in motd


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


def test_parse_motd_extracts_staging_and_channel():
    raw = (
        "<color=0xffffffff><br>FC: "
        "<url=showinfo:1377//90000001>Securitas Protector</url><br>"
        "Staging: <url=showinfo:5//30000142>Jita</url><br>"
        "Doctrine: Shield HACs<br>"
        "DPS: <url=fitting:12015:2185;5::>Arty Muninn</url><br>"
        "Logi: <url=joinChannel:player_-84651075//None//None>Cap Chain</url>"
        "</color>"
    )
    out = parse_motd(raw)
    # Staging from the FIRST showinfo:5 (Solar System typeID) link.
    assert out["staging"] == {"system_id": 30000142, "name": "Jita"}
    # Channel from the FIRST joinChannel link: raw token id, display name.
    assert out["channel"]["name"] == "Cap Chain"
    assert out["channel"]["id"] == "player_-84651075//None//None"
    # FC + fittings unchanged by the new extraction.
    assert out["fc"]["character_id"] == 90000001
    assert {f["dna"] for f in out["fittings"]} == {"12015:2185;5::"}


def test_parse_motd_staging_only_first_system_link():
    raw = (
        "Staging: <url=showinfo:5//30000142>Jita</url> "
        "alt <url=showinfo:5//30002187>Amarr</url>"
    )
    out = parse_motd(raw)
    assert out["staging"] == {"system_id": 30000142, "name": "Jita"}


def test_parse_motd_channel_bare_builtin_id():
    raw = "Logi: <url=joinChannel:2>Help</url>"
    out = parse_motd(raw)
    assert out["channel"] == {"name": "Help", "id": "2"}


def test_parse_motd_no_staging_or_channel_is_none():
    # A character showinfo link (type 1377) is NOT a staging system link.
    raw = "<url=showinfo:1377//90000001>Securitas Protector</url> just text"
    out = parse_motd(raw)
    assert out["staging"] is None
    assert out["channel"] is None


def test_parse_motd_extracts_fits_from_anchor_form():
    # The EVE client's native anchor form, as read back via ESI.
    raw = ('DPS: <a href="fitting:12015:2185;5::">Arty Muninn</a> '
           '<a href="fitting:11985::">Shield Scimitar</a>')
    out = parse_motd(raw)
    assert {f["dna"] for f in out["fittings"]} == {"12015:2185;5::", "11985::"}


def test_parse_motd_anchor_fc_staging_channel():
    raw = (
        'FC: <a href="showinfo:1377//90000001">Securitas Protector</a><br>'
        'Staging: <a href="showinfo:5//30000142">Jita</a><br>'
        'Logi: <a href="joinChannel:player_-84651075//None//None">Cap Chain</a>'
    )
    out = parse_motd(raw)
    assert out["fc"]["character_id"] == 90000001
    assert out["staging"] == {"system_id": 30000142, "name": "Jita"}
    assert out["channel"]["id"] == "player_-84651075//None//None"


def test_parse_motd_anchor_single_quoted_href():
    raw = "<a href='fitting:12015:2185;5::'>Arty Muninn</a>"
    out = parse_motd(raw)
    assert {f["dna"] for f in out["fittings"]} == {"12015:2185;5::"}


def test_parse_motd_anchor_font_wrapped_and_entity_decoded():
    # Display names are HTML-entity-encoded and links sit inside <font ...> runs.
    raw = (
        '<font size=14 color=0xffd98d00>'
        'FC: <a href="showinfo:1377//90000001">Securitas &amp; Co</a> '
        'DPS: <a href="fitting:12015:2185;5::">Arty &gt;Muninn&lt;</a>'
        '</font>'
    )
    out = parse_motd(raw)
    assert out["fittings"][0]["name"] == "Arty >Muninn<"
    assert out["fc"]["name"] == "Securitas & Co"


def test_parse_motd_url_form_still_works():
    # Regression: the legacy <url=...> form must parse exactly as before.
    raw = ("Form up<br><url=showinfo:1377//90000001>Securitas Protector</url><br>"
           "<url=fitting:12015:2185;5::>Arty Muninn</url> "
           "<url=fitting:11985::>Shield Scimitar</url>")
    out = parse_motd(raw)
    assert out["fc"]["character_id"] == 90000001
    assert out["fc"]["name"] == "Securitas Protector"
    assert {f["dna"] for f in out["fittings"]} == {"12015:2185;5::", "11985::"}
    assert out["raw"] == raw


def test_build_motd_has_leading_break_by_default():
    motd = build_motd(fc_name="FC", fc_character_id=1, doctrine_name="D",
                      fits_by_tag={"DPS": [("670::", "Pod")]})
    # The content (inside the white wrapper) starts with a <br> so the first
    # line begins on a fresh line in-game.
    assert motd.startswith("<color=0xffffffff><br>")


def test_build_motd_leading_break_can_be_disabled():
    motd = build_motd(fc_name="FC", fc_character_id=1, doctrine_name="D",
                      fits_by_tag={"DPS": [("670::", "Pod")]}, leading_break=False)
    assert not motd.startswith("<color=0xffffffff><br>")
    assert motd.startswith("<color=0xffffffff>FC:")
