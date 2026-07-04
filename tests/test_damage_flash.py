"""Tests for damage_flash.DamageFlashTracker — pure, injectable clock, no Tk (Task B6)."""
import damage_flash as df


def _cfg(pct=10, window=5, cooldown=3, reference="weakest"):
    # These legacy cases exercise the pct-of-reference THRESHOLD path, so pin the
    # mode explicitly — the DEFAULT is now 'any' (see the any-mode tests below).
    return {"damage_flash_mode": "threshold",
            "damage_flash_pct": pct, "damage_flash_window_s": window,
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


def test_unknown_hp_never_flashes_in_threshold_mode_degrades_to_any():
    # In explicit THRESHOLD mode, unknown HP must NOT silently return False
    # (the root-cause of the missed death flash) — it degrades to any-damage.
    t = df.DamageFlashTracker()
    t.add("k", 5, now=100.0)
    cfg = _cfg()
    cfg["damage_flash_mode"] = "threshold"
    assert t.should_flash("k", None, cfg, now=100.1)               # degraded → any dmg
    t2 = df.DamageFlashTracker()
    t2.add("k", 7, now=100.0)
    cfg2 = _cfg()
    cfg2["damage_flash_mode"] = "threshold"
    assert t2.should_flash("k", {"shield": None, "armor": None, "hull": None},
                           cfg2, now=100.1)                        # degraded → any dmg


# ── ANY-DAMAGE mode (the new DEFAULT; log-only, no HP, no ESI) ───────────────

def _any_cfg(window=5, cooldown=3):
    # No damage_flash_mode key at all → must default to 'any'.
    return {"damage_flash_window_s": window, "damage_flash_cooldown_s": cooldown}


def test_absent_mode_defaults_to_any_and_flashes_on_any_incoming_damage():
    t = df.DamageFlashTracker()
    t.add("k", 1, now=100.0)                       # a single point of damage
    # hp=None, no reference, no threshold — 'any' fires purely on windowed dmg>0.
    assert t.should_flash("k", None, _any_cfg(), now=100.1)


def test_any_mode_no_damage_does_not_flash():
    t = df.DamageFlashTracker()
    assert not t.should_flash("k", None, _any_cfg(), now=100.1)     # nothing added


def test_any_mode_death_repro_flashes_on_the_six_real_death_amounts():
    # The exact failure: six incoming hits with UNKNOWN hp (ESI HP absent) must
    # each flash under the any-damage default. Pre-fix these were all suppressed.
    death_amounts = [62, 333, 88, 547, 120, 41]
    for i, amt in enumerate(death_amounts):
        t = df.DamageFlashTracker()
        t.add("kirejen", amt, now=100.0 + i)
        cfg = {"damage_flash_mode": "any", "damage_flash_window_s": 5,
               "damage_flash_cooldown_s": 3}
        assert t.should_flash("kirejen", None, cfg, now=100.1 + i), amt


def test_any_mode_window_expiry_stops_flash():
    t = df.DamageFlashTracker()
    t.add("k", 500, now=100.0)
    assert not t.should_flash("k", None, _any_cfg(window=5), now=106.5)  # aged out


def test_any_mode_cooldown_gates_second_flash():
    t = df.DamageFlashTracker()
    t.add("k", 50, now=100.0)
    assert t.should_flash("k", None, _any_cfg(cooldown=3), now=100.1)     # fires
    t.add("k", 50, now=101.0)
    assert not t.should_flash("k", None, _any_cfg(cooldown=3), now=101.1)  # within cd
    assert t.should_flash("k", None, _any_cfg(cooldown=3), now=103.6)      # cd elapsed


def test_threshold_mode_with_known_hp_unchanged():
    # Explicit threshold mode + known HP behaves exactly as the legacy engine.
    t = df.DamageFlashTracker()
    cfg = _cfg()
    cfg["damage_flash_mode"] = "threshold"
    t.add("k", 120, now=100.0)
    assert not t.should_flash("k", HP, cfg, now=100.1)   # 120 < 200 (10% of 2000)
    t.add("k", 100, now=101.0)
    assert t.should_flash("k", HP, cfg, now=101.1)       # sum 220 >= 200
