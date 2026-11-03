"""Frozen extension protocol helpers; no model loading or generation is performed."""
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re


SCORE_LINE = re.compile(r"^Score of the Assistant\s+([0-9]+)\s*:\s*(.*?)\s*$", re.I)
SYSTEM = "You are a helpful and precise assistant for checking the quality of the answer."
AUTHOR_EC_INSTRUCTION = """We would like to request your feedback on the performance of two AI assistants in response to the user question displayed above.
    Please rate the helpfulness, relevance, accuracy, level of details of their responses. 

    Each assistant receives an overall score on a scale of 1 to 10, where a higher score indicates better overall performance.
    Please first provide a comprehensive explanation of your evaluation, avoiding any potential bias and ensuring that the order in which the responses were presented does not affect your judgment. 
    Then, output two lines indicating the scores for Assistant 1 and 2, respectively.

    Output with the following format:
    Evaluation evidence: <your evluation explanation here>
    Score of the Assistant 1: <score>
    Score of the Assistant 2: <score>"""
CRITERIA = {
    "FairEval": "Please rate the helpfulness, relevance, accuracy, level of details of their responses.",
    "LLMBar": (
        "Please rate how faithfully and correctly each response follows the user instruction. "
        "An attractive response that deviates from the instruction should receive a lower score."
    ),
}


def render_extension_prompt(question, answer_x, answer_y, layout, dataset):
    """Only the displayed order, identity labels and requested score-line order vary."""
    answers = {"X": answer_x, "Y": answer_y}
    chunks = [f"[Question]\n{question}\n\n"]
    for identity in layout["display_content_order"]:
        label = layout["labels"][identity]
        chunks.append(
            f"[The Start of Assistant {label}'s Answer]\n{answers[identity]}\n"
            f"[The End of Assistant {label}'s Answer]\n\n"
        )
    instruction = AUTHOR_EC_INSTRUCTION.replace(CRITERIA['FairEval'], CRITERIA[dataset])
    if layout['score_output_label_order'] == [2, 1]:
        instruction = instruction.replace('Assistant 1 and 2, respectively.', 'Assistant 2 and 1, respectively.')
        instruction = instruction.replace(
            '    Score of the Assistant 1: <score>\n    Score of the Assistant 2: <score>',
            '    Score of the Assistant 2: <score>\n    Score of the Assistant 1: <score>',
        )
    chunks.append('[System]\n' + instruction + '\n')
    return SYSTEM, ''.join(chunks)


def sampling_seed(model_revision, question_id, block, layout_id, draw):
    key = f"faireval-v3.1|{model_revision}|{question_id}|{block}|{layout_id}|{draw}"
    return int.from_bytes(sha256(key.encode("utf8")).digest()[:8], "big") % (2**63)


def parse_extension_score(text, layout, truncated=False):
    """Read explicit labels in the terminal score block, never the requested positions."""
    rows = []
    for line in reversed(text.strip().splitlines()):
        if not line.strip():
            continue
        match = SCORE_LINE.fullmatch(line.strip())
        if match is None:
            break
        rows.append((int(match[1]), match[2]))
    rows.reverse()
    reasons = []
    labels = [label for label, _ in rows]
    if len(rows) != 2 or sorted(labels) != [1, 2]:
        reasons.append("missing_duplicate_or_unknown_label_in_final_score_block")
    values = {}
    for label, number in rows:
        try:
            value = Decimal(number)
        except InvalidOperation:
            reasons.append("not_a_number")
            continue
        if not value.is_finite() or not Decimal(1) <= value <= Decimal(10):
            reasons.append("score_outside_1_to_10")
        else:
            values[label] = value
    numeric_valid = not reasons
    if truncated:
        reasons.append("truncated")
    actual_s = None
    if numeric_valid:
        actual_s = 1 if labels[0] == layout["labels"]["X"] else -1
    return {
        "valid": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "assigned_S": layout["S"],
        "actual_S": actual_s,
        "actual_label_order": labels,
        "order_compliant": actual_s == layout["S"] if numeric_valid else None,
        "scores": {
            identity: str(values[label]) for identity, label in layout["labels"].items()
        } if numeric_valid else None,
        "truncated": truncated,
    }


def aggregate_selected(records):
    """All selected calls are required; a failed call does not silently change a method."""
    if any(not row["valid"] for row in records):
        return {"valid": False, "decision": None}
    # Equal call weights, fixed equal counts within each method's selected layouts.
    gap = sum(Decimal(row["scores"]["X"]) - Decimal(row["scores"]["Y"]) for row in records)
    return {
        "valid": True,
        "decision": "X" if gap > 0 else "Y" if gap < 0 else "TIE",
        "mean_score_gap": str(gap / len(records)),
    }
