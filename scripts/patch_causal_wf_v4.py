from __future__ import annotations

from pathlib import Path
import sys

EXTERNAL_BLOCK = '        # Explicit external-liquidity sweeps. Every level existed before this bar;\n        # the Asia range is exposed only after its 08:00 UTC completion.\n        level_pairs = (\n            ("prev_day", r.get("prev_day_low"), r.get("prev_day_high")),\n            ("prev_week", r.get("prev_week_low"), r.get("prev_week_high")),\n            ("asia", r.get("asia_low"), r.get("asia_high")),\n        )\n        for level_name, lo_level, hi_level in level_pairs:\n            if pd.notna(lo_level) and low < float(lo_level) - 0.05 * atr and close > float(lo_level) and close > open_:\n                strength = (float(lo_level) - low) / atr + max(0.0, body_atr)\n                level = max(float(lo_level), (open_ + close) / 2.0)\n                events.append((f"{level_name}_sweep", 1, level, low, float(frame["low"].iloc[max(0, i-6):i+1].min()), strength))\n            if pd.notna(hi_level) and high > float(hi_level) + 0.05 * atr and close < float(hi_level) and close < open_:\n                strength = (high - float(hi_level)) / atr + max(0.0, body_atr)\n                level = min(float(hi_level), (open_ + close) / 2.0)\n                events.append((f"{level_name}_sweep", -1, level, high, float(frame["high"].iloc[max(0, i-6):i+1].max()), strength))\n\n        # A stricter two-bar confirmation arm: bar i-1 removes and reclaims a known\n        # external level; the current completed bar displaces beyond the sweep bar.\n        p = frame.iloc[i - 1]\n        p_atr = float(p["atr"]) if np.isfinite(p["atr"]) else atr\n        for level_name, low_col, high_col in (\n            ("prev_day", "prev_day_low", "prev_day_high"),\n            ("prev_week", "prev_week_low", "prev_week_high"),\n            ("asia", "asia_low", "asia_high"),\n        ):\n            lo_level = p.get(low_col)\n            hi_level = p.get(high_col)\n            if (pd.notna(lo_level) and float(p["low"]) < float(lo_level) - 0.05 * p_atr\n                    and float(p["close"]) > float(lo_level) and close > float(p["high"]) + 0.05 * atr\n                    and body_atr >= 0.35):\n                events.append((f"{level_name}_sweep_confirm", 1, (float(p["open"]) + float(p["close"])) / 2.0,\n                               float(p["low"]), float(frame["low"].iloc[max(0, i-7):i+1].min()),\n                               (close - float(p["high"])) / atr + body_atr))\n            if (pd.notna(hi_level) and float(p["high"]) > float(hi_level) + 0.05 * p_atr\n                    and float(p["close"]) < float(hi_level) and close < float(p["low"]) - 0.05 * atr\n                    and body_atr >= 0.35):\n                events.append((f"{level_name}_sweep_confirm", -1, (float(p["open"]) + float(p["close"])) / 2.0,\n                               float(p["high"]), float(frame["high"].iloc[max(0, i-7):i+1].max()),\n                               (float(p["low"]) - close) / atr + body_atr))\n\n'
OLD_VARIANTS = '    variants = []\n    variant_id = 0\n    for entry_mode in (0, 1):\n        for stop_mult in (0.8, 1.2):\n            for target_rr in (1.0, 1.5, 2.0):\n                for max_hold in (24, 48):\n                    variants.append((variant_id, entry_mode, stop_mult, target_rr, max_hold, 6 if entry_mode else 1))\n                    variant_id += 1\n'
NEW_VARIANTS = '    variants = []\n    variant_id = 0\n    # Curated wide-stop / attainable-target arms. Wider structural stops reduce\n    # fee-to-risk distortion, while 0.5R-1.0R exits test whether the previously\n    # observed high-win-rate scenes can retain positive geometric growth.\n    risk_reward_arms = (\n        (0.9, 0.50, 12),\n        (1.2, 0.60, 18),\n        (1.5, 0.75, 24),\n        (2.0, 0.75, 36),\n        (2.5, 0.75, 48),\n        (1.2, 1.00, 24),\n        (1.5, 1.00, 36),\n        (2.0, 1.00, 48),\n        (1.2, 1.50, 48),\n    )\n    for entry_mode in (0, 1):\n        for stop_mult, target_rr, max_hold in risk_reward_arms:\n            variants.append((variant_id, entry_mode, stop_mult, target_rr, max_hold, 6 if entry_mode else 1))\n            variant_id += 1\n'

def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_causal_wf_v4.py SCRIPT")
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    marker = "        for n in (24, 96):\n"
    if marker not in text:
        raise RuntimeError("event insertion marker missing")
    text = text.replace(marker, EXTERNAL_BLOCK + marker, 1)
    if OLD_VARIANTS not in text:
        raise RuntimeError("variant replacement block missing")
    text = text.replace(OLD_VARIANTS, NEW_VARIANTS, 1)
    replacements = {
        "n_estimators=260,": "n_estimators=110,",
        "n_estimators=220,": "n_estimators=90,",
        "if len(train_base) > 700_000:": "if len(train_base) > 300_000:",
        "train_base = train_base.sample(700_000,": "train_base = train_base.sample(300_000,",
        "if len(train_full) > 850_000:": "if len(train_full) > 350_000:",
        "train_full = train_full.sample(850_000,": "train_full = train_full.sample(350_000,",
        'for mode in ("mean", "tail"):\n        for lookback in (18, 30, 42):': 'for mode in ("mean",):\n        for lookback in (30,):',
        "random_policies(procedures, order_rates, 700)": "random_policies(procedures, order_rates, 260)",
    }
    for old, new in replacements.items():
        if old not in text:
            raise RuntimeError(f"patch anchor missing: {old}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")

if __name__ == "__main__":
    main()
