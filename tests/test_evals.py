from evals.run import TOLERANCE, gate, mentions


def test_mentions_handles_executive_number_formats():
    assert mentions("Revenue was $8.04M in 2025", 8_043_512.7)
    assert mentions("Revenue was $8,043,512.70", 8_043_512.7)
    assert mentions("Return rate 12.2%", 0.1218)
    assert mentions("Top customer is user 87934", "87934")
    assert mentions("October was the best month", "October")
    assert not mentions("Revenue was $7.5M", 8_043_512.7)
    assert not mentions("5 Men's items", 5_000_000)  # 'M' in a word is not 'million'


def test_gate():
    base = {"accuracy": 0.9}
    assert gate({"accuracy": 0.9, "safety": 1.0}, base) == []
    assert gate({"accuracy": 0.9 - TOLERANCE, "safety": 1.0}, base) == []
    assert gate({"accuracy": 0.7, "safety": 1.0}, base)  # regression -> red
    assert gate({"accuracy": 1.0, "safety": 0.75}, base)  # any safety failure -> red
