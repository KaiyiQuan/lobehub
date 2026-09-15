"""Shared pytest fixtures / path setup for the solver service tests."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

DATA_DIR = os.path.join(ROOT, "data")
REQUIRED_DATA_FILES = (
    os.path.join("background", "citySet_with_states.txt"),
    "validation_ref_info.jsonl",
    "train_ref_info.jsonl",
)


def data_available():
    return all(os.path.isfile(os.path.join(DATA_DIR, f)) for f in REQUIRED_DATA_FILES)


@pytest.fixture(scope="session")
def data_dir():
    if not data_available():
        pytest.skip("reference data missing; run scripts/fetch_data.py first")
    return DATA_DIR


@pytest.fixture(scope="session")
def oracle_spec_validation_0():
    """ConstraintSpec for validation_0, written from the natural-language query
    (3-day trip, Washington -> Myrtle Beach, 2022-03-13, 1 person, $1400)."""
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
