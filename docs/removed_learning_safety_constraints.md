# Removed Learning Safety Constraints

This note records the learning-time safety constraints that were disabled on
2026-05-04. They were used only in `tools/strategy_preflop_multiway.py` for
blueprint CFR training/export, not as poker engine legality rules.

## Strategy Probability Multipliers

`apply_learning_safety_to_strategy()` multiplied action probabilities by the
following factors and then normalized the strategy:

- `air + jam + dry + call`: `0.10`
- `air + jam + connected/two_tone + call`: `0.18`
- `air + jam + other texture + call`: `0.25`
- `marginal + jam + dry + call`: `0.45`
- `air + jam + all-in`: `0.003`
- `air + jam + raise_large`: `0.01`
- `air + jam + raise_medium`: `0.025`
- `air + jam + raise_small`: `0.06`
- `air + large + all-in`: `0.01`
- `air + large + raise_large`: `0.06`
- `marginal + jam + all-in`: `0.025`
- `marginal + jam + raise_large`: `0.12`
- `draw + jam + non-wet board + all-in`: `0.08`
- `4+ players and not monster/made/strong_pair/combo_draw + all-in`: `0.12`

This was applied during current strategy selection, rollout strategy selection,
uniform/initial fallback strategy selection, and final blueprint export.

## Utility Caps Before Regret Updates

`apply_learning_safety_to_utilities()` capped action utilities before regret
updates with:

- `fold_check_baseline`: best utility among `fold`/`check`
- `safe_baseline`: best utility among `fold`/`check`/`call`
- `penalty_unit`: `max(8.0, min(pot, (to_call + pot) * 0.30))`

The caps were:

- `air + jam + dry + call`: `fold_check_baseline - penalty_unit * 0.25`
- `air + jam + connected/two_tone + call`: `fold_check_baseline + penalty_unit * 0.10`
- `air + jam + other texture + call`: `fold_check_baseline + penalty_unit * 0.20`
- `marginal + jam + dry + call`: `fold_check_baseline + penalty_unit * 0.25`
- `air + jam + all-in`: `safe_baseline - penalty_unit * 2.8`
- `air + jam + raise_large`: `safe_baseline - penalty_unit * 2.2`
- `air + jam + raise_medium`: `safe_baseline - penalty_unit * 1.6`
- `air + jam + raise_small`: `safe_baseline - penalty_unit * 1.1`
- `air + large + all-in`: `safe_baseline - penalty_unit * 2.0`
- `air + large + raise_large`: `safe_baseline - penalty_unit * 1.4`
- `marginal + jam + all-in`: `safe_baseline - penalty_unit * 1.4`
- `marginal + jam + raise_large`: `safe_baseline - penalty_unit * 0.8`
- `draw + jam + non-wet board + all-in`: `safe_baseline - penalty_unit * 0.9`
- `4+ players and not monster/made/strong_pair/combo_draw + all-in`: `safe_baseline - penalty_unit * 0.9`

## Still Kept

The action abstraction rule remains:

- The engine still allows all-in as a legal poker action.
- The blueprint trainer and Pluribus strategy abstraction include `all-in` only
  when `bet_large`/`raise_large` is capped by stack.

The runtime Pluribus preflop all-in guard in `app/sample_cpus/pluribus_agent.py`
was not removed in this change.
