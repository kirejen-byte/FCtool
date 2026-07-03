"""Tests for ship_classes.get_layer_hp — base dogma layer HP (263/265/9),
fake ESI fetch (no network), cache + fail-soft (Task B6)."""
import ship_classes


class _FakeResp:
    def __init__(self, payload, ok=True):
        self._p, self.ok = payload, ok

    def json(self):
        return self._p


def _types_payload(attrs):
    # ESI /universe/types/{id}/ shape: dogma_attributes = [{attribute_id, value}, ...]
    return {"group_id": 26,
            "dogma_attributes": [{"attribute_id": a, "value": v}
                                 for a, v in attrs.items()]}


def test_get_layer_hp_reads_dogma_263_265_9(monkeypatch):
    # 263 shieldCapacity, 265 armorHP, 9 hp (structure)
    payload = _types_payload({263: 4200.0, 265: 3100.0, 9: 2800.0})
    monkeypatch.setattr(ship_classes.requests, "get",
                        lambda *a, **k: _FakeResp(payload))
    ship_classes._layer_hp_cache.clear()
    assert ship_classes.get_layer_hp(587) == {"shield": 4200.0,
                                              "armor": 3100.0, "hull": 2800.0}


def test_get_layer_hp_is_none_safe_on_missing_attrs(monkeypatch):
    payload = _types_payload({9: 1500.0})   # only structure present
    monkeypatch.setattr(ship_classes.requests, "get",
                        lambda *a, **k: _FakeResp(payload))
    ship_classes._layer_hp_cache.clear()
    hp = ship_classes.get_layer_hp(588)
    assert hp == {"shield": None, "armor": None, "hull": 1500.0}


def test_get_layer_hp_caches_and_survives_fetch_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(ship_classes.requests, "get", boom)
    ship_classes._layer_hp_cache.clear()
    assert ship_classes.get_layer_hp(999) is None
