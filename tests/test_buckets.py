from polyweather.pricing.buckets import bucket_prob, parse_bucket


def test_parse_exact():
    assert parse_bucket("25°C") == (25.0, 25.0)
    assert parse_bucket("25C") == (25.0, 25.0)
    assert parse_bucket("-3°C") == (-3.0, -3.0)


def test_parse_open_ends():
    lo, hi = parse_bucket("24°C or below")
    assert lo == float("-inf") and hi == 24.0
    lo, hi = parse_bucket("30°C or higher")
    assert lo == 30.0 and hi == float("inf")
    lo, hi = parse_bucket("30°C or above")
    assert lo == 30.0


def test_parse_range():
    assert parse_bucket("26-27°C") == (26.0, 27.0)


def test_parse_garbage():
    assert parse_bucket("Will it rain?") is None


def test_bucket_prob():
    pmf = {24: 0.2, 25: 0.5, 26: 0.3}
    assert abs(bucket_prob(pmf, 25, 25) - 0.5) < 1e-9
    assert abs(bucket_prob(pmf, float("-inf"), 24) - 0.2) < 1e-9
    assert abs(bucket_prob(pmf, 25, float("inf")) - 0.8) < 1e-9
