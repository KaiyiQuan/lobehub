"""Tests for loader.load_ref_data / RefDataStore (ported from the experiment solver)."""

import pytest

from solver_service.packs.travelplanner import loader


def test_validation_0_route(data_dir):
    ref = loader.load_ref_data("validation_0", data_dir)
    assert ref.origin == "Washington"
    assert ref.cities == ["Myrtle Beach"]
    assert ref.days == 3
    assert [leg.origin for leg in ref.legs] == ["Washington", "Myrtle Beach"]
    assert [leg.dest for leg in ref.legs] == ["Myrtle Beach", "Washington"]
    assert ref.legs[0].date == "2022-03-13"
    assert ref.legs[1].date == "2022-03-15"


def test_validation_0_leg_options(data_dir):
    ref = loader.load_ref_data("validation_0", data_dir)
    for leg in ref.legs:
        assert len(leg.flights) == 2
        assert leg.self_driving is not None
        assert leg.self_driving.cost == 34
        assert leg.self_driving.distance_km == 693
        assert "6 hours 4" in leg.self_driving.duration
        assert leg.taxi is not None
        assert leg.taxi.cost == 693
        # verbatim strings are kept for plan rendering
        assert leg.self_driving.raw.startswith("self-driving, from ")


def test_validation_0_entities(data_dir):
    ref = loader.load_ref_data("validation_0", data_dir)
    assert len(ref.attractions["Myrtle Beach"]) == 20
    assert len(ref.restaurants["Myrtle Beach"]) == 28
    assert len(ref.accommodations["Myrtle Beach"]) == 19
    rest = ref.restaurants["Myrtle Beach"][0]
    assert set(["Name", "Average Cost", "Cuisines", "Aggregate Rating", "City"]) <= set(rest)
    acc = ref.accommodations["Myrtle Beach"][0]
    assert set(["NAME", "price", "room type", "house_rules", "minimum nights", "maximum occupancy"]) <= set(acc)


def test_city_state_map(data_dir):
    ref = loader.load_ref_data("validation_0", data_dir)
    assert ref.city_state["Myrtle Beach"] == "South Carolina"
    assert ref.city_state["Washington"] == "District of Columbia"
    # 311 lines in the file but 312 unique city->state entries (last line has
    # no trailing newline, so wc -l undercounts by one)
    assert len(ref.city_state) == 312


def test_bad_query_id(data_dir):
    for bad in ("foo", "validation_x", "nope_0"):
        with pytest.raises(loader.RefDataError):
            loader.load_ref_data(bad, data_dir)


def test_ref_data_store_matches_direct_load(data_dir):
    store = loader.RefDataStore(data_dir, splits=("validation",))
    via_store = store.get("validation_0")
    direct = loader.load_ref_data("validation_0", data_dir)
    assert via_store.origin == direct.origin
    assert via_store.cities == direct.cities
    assert via_store.days == direct.days
    assert via_store.raw == direct.raw
    assert store.splits == ["validation"]
    assert store.query_count() == {"validation": 180}


def test_ref_data_store_errors(data_dir):
    store = loader.RefDataStore(data_dir, splits=("validation",))
    with pytest.raises(loader.RefDataError):
        store.get("train_0")  # split not loaded
    with pytest.raises(loader.RefDataError):
        store.get("validation_9999")  # out of range
    with pytest.raises(loader.RefDataError):
        store.get("nope_0")
