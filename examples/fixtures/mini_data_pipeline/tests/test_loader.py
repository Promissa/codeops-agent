from mini_data_pipeline.loader import load_csv


def test_load_csv_reads_file(tmp_path):
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("name,age\nAda,37\n")

    assert load_csv(csv_path) == [["name", "age"], ["Ada", "37"]]
