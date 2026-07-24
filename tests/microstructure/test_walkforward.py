from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ictbt.microstructure.walkforward import (
    economic_probability_threshold,
    fit_l2_logistic,
    purged_expanding_monthly_predict,
)


def test_l2_logistic_learns_direction_without_external_ml_dependency() -> None:
    index = pd.Index([f"row-{item}" for item in range(40)])
    x = np.linspace(-2.0, 2.0, len(index))
    features = pd.DataFrame({"flow": x, "noise": np.sin(x)}, index=index)
    labels = pd.Series((x > 0).astype(float), index=index)
    model = fit_l2_logistic(features, labels)
    probabilities = model.predict_proba(features)

    assert model.feature_names == ("flow", "noise")
    assert probabilities.iloc[:10].mean() < 0.3
    assert probabilities.iloc[-10:].mean() > 0.7
    assert model.iterations <= 100


def test_economic_threshold_uses_train_payoff_not_test_optimization() -> None:
    returns = pd.Series([2.0, 2.0, 1.0, -1.0, -1.0, -1.0])
    # average win 5/3, average loss -1 => 1 / (5/3 + 1) = 0.375
    assert economic_probability_threshold(returns) == pytest.approx(0.375)


def panel() -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series]:
    known = pd.date_range("2022-07-01", "2023-03-01", freq="2D", tz="UTC", inclusive="left")
    index = pd.Index([f"scene-{item:03d}" for item in range(len(known))])
    base = np.arange(len(known), dtype=float)
    signal = np.sin(base * 0.7)
    net_r = pd.Series(np.where(signal >= 0, 1.2, -0.8), index=index)
    features = pd.DataFrame(
        {
            "signed_flow": signal,
            "efficiency": np.cos(base * 0.3),
            "distance": (base % 7) / 7.0,
        },
        index=index,
    )
    known_series = pd.Series(known, index=index)
    label_end = known_series + pd.to_timedelta((base % 4) + 1, unit="D")
    return features, net_r, known_series, label_end


def test_monthly_predictions_use_only_outcomes_before_purged_cutoff() -> None:
    features, net_r, known, label_end = panel()
    result = purged_expanding_monthly_predict(
        features,
        net_r,
        known,
        label_end,
        test_start="2023-01-01",
        test_end="2023-03-01",
        embargo=pd.Timedelta(days=2),
        minimum_train_samples=40,
    )

    assert len(result.folds) == 2
    assert not result.predictions.empty
    for fold in result.folds:
        trainable = (known < fold.test_start) & (label_end < fold.train_cutoff)
        assert fold.train_samples == int(trainable.sum())
        test_rows = result.predictions.loc[
            result.predictions["test_month"] == fold.test_start
        ]
        assert len(test_rows) == fold.test_samples
        assert bool((test_rows["known_at"] >= fold.test_start).all())
        assert bool((test_rows["known_at"] < fold.test_end).all())
        assert bool((test_rows["train_cutoff"] == fold.train_cutoff).all())


def test_future_labels_and_features_cannot_change_earlier_month_prediction() -> None:
    features, net_r, known, label_end = panel()
    original = purged_expanding_monthly_predict(
        features,
        net_r,
        known,
        label_end,
        test_start="2023-01-01",
        test_end="2023-03-01",
        embargo=pd.Timedelta(days=2),
        minimum_train_samples=40,
    )
    future = known >= pd.Timestamp("2023-02-01", tz="UTC")
    changed_features = features.copy()
    changed_returns = net_r.copy()
    changed_features.loc[future, :] = 1_000_000.0
    changed_returns.loc[future] *= -100.0
    repeated = purged_expanding_monthly_predict(
        changed_features,
        changed_returns,
        known,
        label_end,
        test_start="2023-01-01",
        test_end="2023-03-01",
        embargo=pd.Timedelta(days=2),
        minimum_train_samples=40,
    )

    january = original.predictions["test_month"] == pd.Timestamp("2023-01-01", tz="UTC")
    january_ids = original.predictions.index[january]
    assert repeated.predictions.loc[january_ids, "probability"].to_numpy() == pytest.approx(
        original.predictions.loc[january_ids, "probability"].to_numpy()
    )
    assert repeated.predictions.loc[january_ids, "probability_threshold"].to_numpy() == pytest.approx(
        original.predictions.loc[january_ids, "probability_threshold"].to_numpy()
    )


def test_insufficient_or_one_class_training_fails_instead_of_guessing() -> None:
    features, net_r, known, label_end = panel()
    with pytest.raises(ValueError, match="insufficient train samples"):
        purged_expanding_monthly_predict(
            features,
            net_r,
            known,
            label_end,
            test_start="2023-01-01",
            test_end="2023-02-01",
            minimum_train_samples=10_000,
        )

    with pytest.raises(ValueError, match="both binary classes"):
        fit_l2_logistic(features.iloc[:20], pd.Series(1.0, index=features.index[:20]))
