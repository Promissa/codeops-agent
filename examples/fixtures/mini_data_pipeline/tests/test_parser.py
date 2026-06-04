from mini_data_pipeline.parser import normalize_field, parse_csv


def test_parse_csv_normal_rows():
    assert parse_csv("name,age\nAda,37\n") == [["name", "age"], ["Ada", "37"]]


def test_null_value_returns_none():
    assert normalize_field("NULL") is None
