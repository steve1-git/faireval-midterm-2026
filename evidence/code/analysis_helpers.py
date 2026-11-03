"""Secondary diagnostics using existing generations; no model calls or label-based tuning."""
from decimal import Decimal
from itertools import combinations
from collections import Counter
import math

TERMS = ('P', 'L', 'S', 'PL', 'PS', 'LS', 'PLS')


def decision(gap):
    return 'X' if gap > 0 else 'Y' if gap < 0 else 'TIE'


def question_sensitivity(layouts, blocks):
    """Three complete draw1 blocks, eight assigned layouts per block, for ONE question.

    Conflict-pair counts are diagnostics, never independent question counts.
    The same complete-question set supports both cross-layout and repeat comparisons.
    """
    layout_by_id = {row['layout']: row for row in layouts}
    valid_cells = sum(block[key]['valid'] for block in blocks for key in layout_by_id)
    result = {'valid_cells': valid_cells, 'scheduled_cells': 24, 'eligible': valid_cells == 24}
    if not result['eligible']:
        return result
    gaps = [
        {key: Decimal(block[key]['scores']['X']) - Decimal(block[key]['scores']['Y'])
         for key in layout_by_id}
        for block in blocks
    ]
    coefficients = []
    for gap in gaps:
        coefficients.append({
            term: float(sum(
                math.prod(row[sign] for sign in term) * gap[key]
                for key, row in layout_by_id.items()
            ) / 8)
            for term in TERMS
        })
    result['coefficients_by_block'] = coefficients
    result['question_mean_coefficients'] = {
        term: sum(row[term] for row in coefficients) / 3 for term in TERMS
    }
    predictions = [{key: decision(value) for key, value in gap.items()} for gap in gaps]
    repeat_events = [
        (predictions[b][key], predictions[c][key])
        for key in layout_by_id for b, c in combinations(range(3), 2)
    ]

    def event_summary(pairs):
        conflicts = sum(x != y for x, y in pairs)
        opposite_winners = sum({x, y} == {'X', 'Y'} for x, y in pairs)
        tie_transitions = sum(x != y and 'TIE' in (x, y) for x, y in pairs)
        return {'pairs': len(pairs), 'conflicts': conflicts,
                'opposite_winners': opposite_winners, 'tie_transitions': tie_transitions,
                'conflict_rate': conflicts / len(pairs)}

    result['same_layout_repeat'] = event_summary(repeat_events)
    result['single_factor_flip'] = {}
    for factor in ('P', 'L', 'S'):
        edges = []
        for plus in layouts:
            if plus[factor] != 1:
                continue
            minus = next(row for row in layouts if all(
                row[f] == (-plus[f] if f == factor else plus[f]) for f in ('P', 'L', 'S')
            ))
            edges.append((plus['layout'], minus['layout']))
        events = [(predictions[b][a], predictions[b][z])
                  for a, z in edges for b in range(3)]
        summary = event_summary(events)
        summary['minus_repeat_conflict_rate'] = summary['conflict_rate'] - result['same_layout_repeat']['conflict_rate']
        result['single_factor_flip'][factor] = summary
    return result


def skip_invalid_diagnostic(parsed_records):
    """Only the failure aggregation changes; use the SAME strict parser and validity.

    A reduced set of calls is explicitly a diagnostic, not the full A/B/C/D protocol.
    """
    valid = [row for row in parsed_records if row['valid']]
    retained_layouts = Counter(row['layout'] for row in valid)
    counts = {'scheduled_calls': len(parsed_records), 'retained_calls': len(valid),
              'skipped_calls': len(parsed_records) - len(valid),
              'retained_calls_by_layout': dict(retained_layouts),
              'missing_required_layouts': sorted({row['layout'] for row in parsed_records} - set(retained_layouts))}
    if not valid:
        return {**counts, 'valid': False, 'decision': None, 'reason': 'no_valid_scores'}
    gap = sum(Decimal(row['scores']['X']) - Decimal(row['scores']['Y']) for row in valid)
    return {**counts, 'valid': True, 'decision': decision(gap), 'mean_gap': str(gap / len(valid))}


def author_parse_scores(review):
    """Same numeric return semantics as the author's parser; omit only its print."""
    try:
        score1 = review.split('\n')[-2].split(':')[-1].strip()
        score2 = review.split('\n')[-1].split(':')[-1].strip()
        return [float(score1), float(score2)]
    except (IndexError, ValueError):
        return [-1, -1]


def author_compatible_aggregate(raw_records):
    """Diagnostic for original EC/MEC/BPC standard forward/reverse prompts only.

    Each item supplies untouched 'text' and 'label_X'. Original positional parsing,
    -1 skipping, and float means are preserved. Nonfinite/all-skipped cases are
    recorded rather than turned into a usable scientific decision.
    """
    scores = []
    retained_orientations = Counter()
    for row in raw_records:
        one, two = author_parse_scores(row['text'])
        if one == -1 or two == -1:
            continue
        scores.append((one, two) if row['label_X'] == 1 else (two, one))
        retained_orientations['forward' if row['label_X'] == 1 else 'reverse'] += 1
    counts = {'scheduled_calls': len(raw_records), 'retained_calls': len(scores),
              'skipped_calls': len(raw_records) - len(scores),
              'retained_calls_by_standard_orientation': dict(retained_orientations)}
    if not scores:
        return {**counts, 'valid': False, 'decision': None,
                'author_behavior': 'division_by_zero_if_run_unmodified'}
    x = sum(row[0] for row in scores) / len(scores)
    y = sum(row[1] for row in scores) / len(scores)
    branch = 'TIE' if x == y else 'X' if x > y else 'Y'
    finite = math.isfinite(x) and math.isfinite(y)
    return {**counts, 'valid': finite, 'decision': branch if finite else None,
            'raw_score_means': [repr(x), repr(y)], 'literal_author_final_branch': branch,
            'author_behavior': 'finite_score_means' if finite else 'nonfinite_not_usable_for_scientific_accuracy'}
