#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np

MODULE_PATH = Path(__file__).with_name('state_first_l1_clean_v2.py')
spec = importlib.util.spec_from_file_location('state_first_l1_clean_v2_impl', MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError(MODULE_PATH)
impl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(impl)

_original_build_day = impl.build_day


def _fail_closed_build_day(symbol: str, day: str, data_dir: Path):
    panel, sources = _original_build_day(symbol, day, data_dir)
    valid = panel.entry_time_ms.to_numpy(np.int64) >= 0
    return panel.loc[valid].copy(), sources


def _strict_evaluate_command(args):
    """Evaluate with every trading threshold known before selection starts."""
    args.output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(args.input_dir.rglob('*_state_first_l1_v2.csv.gz'))
    if len(files) != 2:
        raise ValueError(f'expected two symbol panels, got {files}')
    d = impl.pd.concat(
        [impl.pd.read_csv(p) for p in files], ignore_index=True
    ).sort_values(['entry_time_ms', 'symbol'], kind='mergesort').reset_index(drop=True)
    X = d[impl.FEATURES].replace([np.inf, -np.inf], np.nan)
    train = d.day.eq(impl.DAYS[0])
    train_arr = train.to_numpy()
    models = {
        'ridge': lambda: impl.make_pipeline(
            impl.SimpleImputer(strategy='median'),
            impl.StandardScaler(),
            impl.Ridge(alpha=100.0),
        ),
        'hist': lambda: impl.make_pipeline(
            impl.SimpleImputer(strategy='median'),
            impl.HistGradientBoostingRegressor(
                max_iter=160,
                max_leaf_nodes=15,
                l2_regularization=30.0,
                learning_rate=0.04,
                random_state=2407,
            ),
        ),
        'extra': lambda: impl.make_pipeline(
            impl.SimpleImputer(strategy='median'),
            impl.ExtraTreesRegressor(
                n_estimators=240,
                min_samples_leaf=80,
                max_features=0.7,
                n_jobs=-1,
                random_state=2407,
            ),
        ),
    }
    predictions = {}
    for h in impl.HORIZONS:
        y = impl.pd.to_numeric(d[f'mid_log_{h}'], errors='coerce')
        ok = train & y.notna()
        for name, maker in models.items():
            model = maker()
            model.fit(X.loc[ok], y.loc[ok])
            predictions[(name, h)] = model.predict(X).astype(float)
    for name, pred in impl.rule_predictions(d).items():
        for h in impl.HORIZONS:
            predictions[(name, h)] = pred

    rows = []
    for (family, h), pred in predictions.items():
        values = np.abs(pred[train_arr])
        values = values[np.isfinite(values) & (values > 0)]
        for quantile in impl.QUANTILES:
            if len(values) < 100:
                continue
            threshold = float(np.quantile(values, quantile))
            rec = {
                'family': family,
                'horizon_s': h,
                'quantile': quantile,
                'threshold': threshold,
                'threshold_calibration_day': impl.DAYS[0],
                'candidate_id': f'{family}|h{h}|q{quantile}',
            }
            for day_tag, day in (
                ('selection', impl.DAYS[1]),
                ('validation', impl.DAYS[2]),
            ):
                for cost in impl.COSTS:
                    mm = impl.metrics(impl.route(d, pred, h, threshold, day, cost))
                    for key, value in mm.items():
                        rec[f'{day_tag}_{int(cost)}_{key}'] = value
            rows.append(rec)

    screen = impl.pd.DataFrame(rows)

    def gate(row):
        for tag in ('selection', 'validation'):
            if row[f'{tag}_12_trades'] < 50 or row[f'{tag}_18_trades'] < 50:
                return False
            if row[f'{tag}_12_log_growth'] <= 0 or row[f'{tag}_18_log_growth'] <= 0:
                return False
            if (
                row[f'{tag}_12_pf'] < 1.10
                or row[f'{tag}_12_top5_share'] > 0.35
                or row[f'{tag}_12_mdd'] > 0.15
            ):
                return False
        return True

    screen['eligible_pretest'] = screen.apply(gate, axis=1) if len(screen) else False
    screen['robust_score'] = (
        np.where(
            screen.eligible_pretest,
            screen[['selection_18_log_growth', 'validation_18_log_growth']].min(axis=1),
            -1e9,
        )
        if len(screen)
        else []
    )
    if len(screen):
        screen = screen.sort_values(
            ['robust_score', 'validation_18_log_growth', 'candidate_id'],
            ascending=[False, False, True],
            kind='mergesort',
        )

    opened = False
    test = None
    if len(screen) and bool(screen.iloc[0].eligible_pretest):
        best = screen.iloc[0]
        pred = predictions[(str(best.family), int(best.horizon_s))]
        opened = True
        test = {}
        for cost in impl.COSTS:
            ledger = impl.route(
                d,
                pred,
                int(best.horizon_s),
                float(best.threshold),
                impl.DAYS[3],
                cost,
            )
            test[str(int(cost))] = impl.metrics(ledger)
            if cost == 12:
                ledger.to_csv(args.output_dir / 'test_ledger.csv', index=False)

    screen.to_csv(args.output_dir / 'screen.csv', index=False)
    summary = {
        'status': 'COMPLETE',
        'contract': 'STATE_FIRST_L1_TRADE_FLOW_V2_STRICT_TRAIN_THRESHOLD',
        'dates': impl.DAYS,
        'threshold_calibration_day': impl.DAYS[0],
        'screened': int(len(screen)),
        'eligible_pretest': int(screen.eligible_pretest.sum()) if len(screen) else 0,
        'test_opened': opened,
        'test': test,
        'strict_target_gate_passed': bool(
            opened
            and test
            and test['12']['g_daily'] >= 0.01
            and test['18']['log_growth'] > 0
            and test['12']['trades'] >= 50
            and test['12']['top5_share'] <= 0.35
        ),
        'promotion_allowed': False,
        'orders_submitted': False,
        'paper_or_live_started': False,
        'best': (
            screen.head(30)
            .replace([np.nan, np.inf, -np.inf], None)
            .to_dict('records')
            if len(screen)
            else []
        ),
    }
    (args.output_dir / 'summary.json').write_text(
        json.dumps(summary, indent=2), encoding='utf-8'
    )
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    'screened',
                    'eligible_pretest',
                    'test_opened',
                    'test',
                    'strict_target_gate_passed',
                )
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


impl.build_day = _fail_closed_build_day
impl.evaluate_command = _strict_evaluate_command

if __name__ == '__main__':
    raise SystemExit(impl.main())
