"""Command-line interface for the two-phase simplex solver.

The CLI accepts Python-literal strings for the LP data, executes the
two-phase simplex algorithm and writes the resulting outputs (final
tableau, degeneracy report, problem report, basic-variable report and
-- when requested -- the pivot log).  If any exception is raised during
execution, no output file is created and the exact Python exception
traceback is printed to stderr.
"""

import argparse
import ast
import json
import os
import pickle
import sys
import traceback
from decimal import Decimal, ROUND_HALF_UP

from simplex.phases import SimplexEngine
from simplex.schema import as_float_tableau

EPS = 1e-9


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------
def parse_list_literal(text):
    """Parse a Python-literal string into the matching data structure."""
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise ValueError("Invalid Python literal: %r" % text) from exc


# ----------------------------------------------------------------------
# Decimal rounding (half-up) and formatting
# ----------------------------------------------------------------------
def _round2(value):
    """Round ``value`` to 2 decimals using half-up rounding.

    A value that rounds to zero (including a negative zero) is normalised
    to a positive ``0.00``.
    """
    d = Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d == 0:
        d = Decimal("0.00")
    return d


def _fmt2(d):
    """Format a 2-decimal ``Decimal`` as a string (preserving sign)."""
    return format(d, ".2f")


def _group_terms(pairs):
    """Group ``(index, value)`` pairs into positive / negative / zero.

    The groups are kept in ascending variable-index order.
    """
    pos, neg, zero = [], [], []
    for index, value in pairs:
        d = _round2(value)
        if d > 0:
            pos.append((index, d))
        elif d < 0:
            neg.append((index, d))
        else:
            zero.append((index, d))
    return pos, neg, zero


def _format_terms(pos, neg, zero):
    """Render the term string.

    Positive terms come first, then negative, then zero.  A leading
    (first) negative term starts as ``-<coefficient>*x<index>`` with no
    separator; later negative terms use `` - <coefficient>*x<index>``.
    Positive and zero terms use `` + `` as the separator (no separator for
    the very first term).  The displayed coefficient is the magnitude.
    """
    parts = []
    emitted = False

    # positive terms: first (if first overall) no separator; rest ' + '
    for i, (index, d) in enumerate(pos):
        term = "%s*x%d" % (_fmt2(abs(d)), index)
        if i == 0 and not emitted:
            parts.append(term)
        else:
            parts.append(" + " + term)
        emitted = True

    # negative terms: the leading (first) negative term has NO separator;
    # later negative terms use ' - '
    for i, (index, d) in enumerate(neg):
        term = "%s*x%d" % (_fmt2(abs(d)), index)
        if i == 0:
            parts.append("-" + term)
        else:
            parts.append(" - " + term)
        emitted = True

    # zero terms: ' + ' separator unless they are the very first term
    for i, (index, d) in enumerate(zero):
        term = "%s*x%d" % (_fmt2(abs(d)), index)
        if i == 0 and not emitted:
            parts.append(term)
        else:
            parts.append(" + " + term)
        emitted = True

    return "".join(parts)


def _objective_line(objective):
    pairs = [(j + 1, objective[j]) for j in range(len(objective))]
    pos, neg, zero = _group_terms(pairs)
    return "maximize " + _format_terms(pos, neg, zero)


def _constraint_line(row, operator, rhs_value):
    pairs = [(j + 1, row[j]) for j in range(len(row))]
    pos, neg, zero = _group_terms(pairs)
    terms = _format_terms(pos, neg, zero)
    rhs_str = _fmt2(_round2(rhs_value))
    return "    " + terms + " " + operator + " " + rhs_str


def _nonnegativity_line(n):
    names = ["x%d" % i for i in range(1, n + 1)]
    return "    " + ", ".join(names) + " >= 0"


def format_problem_report(objective, constraints, operators, rhs):
    n = len(objective)
    m = len(constraints)
    lines = []
    lines.append(_objective_line(objective))
    lines.append("")
    lines.append("subject to:")
    for i in range(m):
        lines.append(_constraint_line(constraints[i], operators[i], rhs[i]))
    lines.append(_nonnegativity_line(n))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Report computation
# ----------------------------------------------------------------------
def _final_tableau(result):
    return as_float_tableau(result["final"])


def _basic_variables(final_tableau, n, k):
    """Return the basic-variable report from the final (phase-2) tableau.

    Exactly one basic variable is reported per constraint row, using the
    first unit column encountered (in column order).
    """
    m = len(final_tableau) - 1  # last row is the objective row
    basic = {}
    for i in range(m):
        row = final_tableau[i]
        for j in range(1, len(row) - 1):
            if abs(row[j] - 1.0) < EPS and all(
                abs(final_tableau[other][j]) < EPS
                for other in range(m)
                if other != i
            ):
                if j <= n:
                    name = "x%d" % j
                else:
                    name = "s%d" % (j - n)
                basic[name] = float(final_tableau[i][-1])
                break
    return basic


def _degenerate(final_tableau):
    m = len(final_tableau) - 1
    for i in range(m):
        if abs(final_tableau[i][-1]) < EPS:
            return True
    return False


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Two-Phase Simplex Algorithm CLI"
    )
    parser.add_argument(
        "--objective_coeff",
        required=True,
        help="Objective coefficients as a Python-literal list of floats",
    )
    parser.add_argument(
        "--constraints_coeff",
        required=True,
        help="Constraint coefficients as a Python-literal list of lists",
    )
    parser.add_argument(
        "--constraints_relational_operators",
        required=True,
        help="Constraint relational operators as a Python-literal list of strings",
    )
    parser.add_argument(
        "--constraints_rhs",
        required=True,
        help="Constraint RHS values as a Python-literal list of floats",
    )
    parser.add_argument(
        "--output_file",
        required=True,
        help="Output file to save the final tableau (pickle)",
    )
    parser.add_argument(
        "--degeneracy_report_file",
        required=True,
        help="Degeneracy report file (pickle)",
    )
    parser.add_argument(
        "--problem_report_file",
        required=True,
        help="Problem report file (UTF-8 text)",
    )
    parser.add_argument(
        "--basic_variables_file",
        required=True,
        help="Basic-variable report file (pickle)",
    )
    parser.add_argument(
        "--initial_pivots",
        default=None,
        help="Optional Python-literal list of Phase-1 pivot dicts",
    )
    parser.add_argument(
        "--pivot_log_file",
        default=None,
        help="Pivot log file (UTF-8 JSON Lines); required with --initial_pivots",
    )

    args = parser.parse_args()

    if args.initial_pivots is not None and args.pivot_log_file is None:
        parser.error("--pivot_log_file is required when --initial_pivots is provided")

    try:
        objective = parse_list_literal(args.objective_coeff)
        constraints = parse_list_literal(args.constraints_coeff)
        operators = parse_list_literal(args.constraints_relational_operators)
        rhs = parse_list_literal(args.constraints_rhs)

        if args.initial_pivots is not None:
            initial_pivots = parse_list_literal(args.initial_pivots)
        else:
            initial_pivots = None

        n = len(objective)
        k = sum(1 for op in operators if op in ("<=", ">="))

        engine = SimplexEngine(
            objective, constraints, operators, rhs, initial_pivots=initial_pivots
        )
        result = engine.solve()

        final_tableau = _final_tableau(result)
        problem_report = format_problem_report(objective, constraints, operators, rhs)
        basic_variables = _basic_variables(final_tableau, n, k)
        degenerate = _degenerate(final_tableau)
        log = result["log"]

        # ---- Write all outputs (only after successful computation). ----
        with open(args.output_file, "wb") as fh:
            pickle.dump(final_tableau, fh)

        with open(args.degeneracy_report_file, "wb") as fh:
            pickle.dump({"degenerate": degenerate}, fh)

        with open(args.problem_report_file, "w", encoding="utf-8") as fh:
            fh.write(problem_report)

        with open(args.basic_variables_file, "wb") as fh:
            pickle.dump({"basic_variables": basic_variables}, fh)

        if args.initial_pivots is not None and args.pivot_log_file is not None:
            with open(args.pivot_log_file, "w", encoding="utf-8") as fh:
                for step, (phase, enter_name, leave_label, post_tableau) in enumerate(
                    log, start=1
                ):
                    entry = {
                        "step": step,
                        "phase": phase,
                        "enter_column": enter_name,
                        "leave_row": leave_label,
                        "tableau": as_float_tableau(post_tableau),
                    }
                    fh.write(json.dumps(entry) + "\n")
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
