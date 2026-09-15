"""Tests for spec.validate_spec (ported from the experiment solver)."""

from solver_service.packs.travelplanner import spec as spec_module


def _valid_spec():
    return {
        "origin": "Washington",
        "destination": {"type": "city", "name": "Myrtle Beach"},
        "days": 3,
        "startDate": "2022-03-13",
        "visitingCityNumber": 1,
        "peopleNumber": 1,
        "budget": 1400,
        "houseRule": None,
        "roomType": None,
        "cuisines": None,
        "transportation": None,
        "soft": None,
    }


def test_valid_oracle_spec_passes():
    assert spec_module.validate_spec(_valid_spec()) == []


def test_valid_state_spec_passes():
    spec = _valid_spec()
    spec.update(
        {
            "destination": {"type": "state", "name": "Texas"},
            "days": 5,
            "visitingCityNumber": 2,
            "houseRule": "pets",
            "roomType": "entire room",
            "cuisines": ["Italian", "BBQ"],
            "transportation": "no flight",
            "soft": {"preferredCuisines": ["BBQ"], "minRestaurantRating": 4.0, "minAccommodationReviewRate": 3.5},
        }
    )
    assert spec_module.validate_spec(spec) == []


def test_missing_required_field_fails_readably():
    spec = _valid_spec()
    del spec["budget"]
    errors = spec_module.validate_spec(spec)
    assert errors
    assert any("budget" in e and "required" in e for e in errors)


def test_bad_enum_fails_readably():
    spec = _valid_spec()
    spec["roomType"] = "king size"
    errors = spec_module.validate_spec(spec)
    assert errors
    assert any("roomType" in e for e in errors)


def test_additional_property_rejected():
    spec = _valid_spec()
    spec["level"] = "easy"  # gold CSV field must never leak into the spec
    errors = spec_module.validate_spec(spec)
    assert errors
    assert any("Additional properties" in e for e in errors)


def test_bad_date_fails():
    spec = _valid_spec()
    spec["startDate"] = "2022-02-30"
    errors = spec_module.validate_spec(spec)
    assert any("startDate" in e for e in errors)


def test_days_visiting_city_number_inconsistent():
    spec = _valid_spec()
    spec["days"] = 4
    errors = spec_module.validate_spec(spec)
    assert any("visitingCityNumber" in e for e in errors)


def test_destination_equal_origin_fails():
    spec = _valid_spec()
    spec["destination"] = {"type": "city", "name": "Washington"}
    errors = spec_module.validate_spec(spec)
    assert any("destination" in e for e in errors)


def test_duplicate_cuisines_rejected():
    spec = _valid_spec()
    spec["cuisines"] = ["Italian", "Italian"]
    errors = spec_module.validate_spec(spec)
    assert any("cuisines" in e for e in errors)


def test_non_dict_spec():
    assert spec_module.validate_spec([1, 2, 3])
