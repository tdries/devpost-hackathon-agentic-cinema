"""What a fix costs, and what the day's budget allows."""
import pytest

from customs import costs
from customs.store import Store


def test_a_patch_is_cents_and_a_bridge_is_euros():
    assert costs.estimate("overlay", 2.0) == 0.04
    assert costs.estimate("bridge", 2.0) > 1.0
    # Veo will not emit less than four seconds, so a two second span and a
    # four second span cost the same
    assert costs.estimate("bridge", 2.0) == costs.estimate("bridge", 4.0)
    assert costs.estimate("bridge", 8.0) > costs.estimate("bridge", 4.0)
    with pytest.raises(ValueError):
        costs.estimate("teleport", 2.0)


def test_a_long_scene_cannot_be_bridged_at_all():
    ok, why = costs.available("bridge", 20.0, spent_today=0.0)
    assert not ok and "longer than Veo will generate" in why
    ok, _ = costs.available("bridge", 8.0, spent_today=0.0)
    assert ok


def test_the_day_s_budget_closes_the_expensive_option_and_leaves_the_cheap_one():
    price = costs.estimate("bridge", 4.0)
    nearly_spent = costs.DAILY_BUDGET_EUR - price + 0.01
    ok, why = costs.available("bridge", 4.0, spent_today=nearly_spent)
    assert not ok and "budget" in why
    # patching never touches the generation budget
    assert costs.available("overlay", 4.0, spent_today=99.0)[0]


def test_every_option_is_priced_and_says_what_it_is_for():
    opts = costs.options(3.0, spent_today=0.0)
    assert [o["key"] for o in opts] == ["overlay", "track", "bridge"]
    for o in opts:
        assert o["eur"] > 0 and o["length"] and o["complexity"] and o["best_for"]
    # tracking is honestly reported as not built rather than quietly offered
    assert not next(o for o in opts if o["key"] == "track")["available"]


def test_three_concrete_choices_per_finding():
    wine = [s["label"] for s in costs.suggestions("alcohol_tobacco_drugs")]
    assert len(wine) == 3
    assert any("non-alcoholic" in s for s in wine)
    assert any("Remove" in s for s in wine)
    # an unmapped dimension still offers three real choices
    assert len(costs.suggestions("something_new")) == 3


def test_spend_is_recorded_per_utc_day(tmp_path):
    store = Store(tmp_path / "s.db")
    assert store.spent_today() == 0.0
    store.record_spend("bridge", 1.88, "run_a", "fnd_a")
    store.record_spend("bridge", 1.88, "run_a", "fnd_b")
    assert store.spent_today() == pytest.approx(3.76)
    # yesterday's spending does not eat today's budget
    store.record_spend("bridge", 4.0, "run_a", "fnd_old", now=0.0)
    assert store.spent_today() == pytest.approx(3.76)
