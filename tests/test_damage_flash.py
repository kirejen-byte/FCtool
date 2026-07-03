"""Tests for damage_flash.DamageFlashTracker — pure, injectable clock, no Tk (Task B6)."""
import damage_flash as df


def _cfg(pct=10, window=5, cooldown=3, reference="weakest"):
    return {"damage_flash_pct": pct, "damage_flash_window_s": window,
            "damage_flash_cooldown_s": cooldown, "damage_flash_reference": reference}


HP = {"shield": 4000.0, "armor": 3000.0, "hull": 2000.0}   # weakest=hull=2000


def test_flashes_when_windowed_damage_crosses_pct_of_weakest_layer():
    t = df.DamageFlashTracker()
    # 10% of weakest (2000) = 200 over 5 s
    t.add("kirejen", 120, now=100.0)
    assert not t.should_flash("kirejen", HP, _cfg(), now=100.1)   # 120 < 200
    t.add("kirejen", 100, now=101.0)                              # sum 220 >= 200
    assert t.should_flash("kirejen", HP, _cfg(), now=101.1)


def test_window_expiry_drops_old_damage():
    t = df.DamageFlashTracker()
    t.add("k", 300, now=100.0)          # would flash alone
    # 6 s later the 100.0 hit has aged out of the 5 s window
    assert not t.should_flash("k", HP, _cfg(window=5), now=106.5)


def test_cooldown_suppresses_a_second_flash():
    t = df.DamageFlashTracker()
    t.add("k", 500, now=100.0)
    assert t.should_flash("k", HP, _cfg(cooldown=3), now=100.1)   # fires (arms cooldown)
    t.add("k", 500, now=101.0)
    assert not t.should_flash("k", HP, _cfg(cooldown=3), now=101.1)  # within 3 s
    assert t.should_flash("k", HP, _cfg(cooldown=3), now=103.6)      # cooldown elapsed


def test_reference_selection():
    t = df.DamageFlashTracker()
    t.add("k", 250, now=100.0)          # 250 dmg
    # total pool = 9000 → 10% = 900 (no flash); hull = 2000 → 10% = 200 (flash)
    assert not t.should_flash("k", HP, _cfg(reference="total"), now=100.1)
    assert t.should_flash("k", HP, _cfg(reference="hull"), now=100.1)
    assert t.should_flash("k", HP, _cfg(reference="shield"), now=100.1) is False  # 400 needed


def test_unknown_hp_never_flashes():
    t = df.DamageFlashTracker()
    t.add("k", 99999, now=100.0)
    assert not t.should_flash("k", None, _cfg(), now=100.1)
    assert not t.should_flash("k", {"shield": None, "armor": None, "hull": None},
                              _cfg(), now=100.1)
