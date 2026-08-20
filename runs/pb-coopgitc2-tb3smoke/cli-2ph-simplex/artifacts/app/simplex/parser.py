"""Two-Phase Simplex CLI.

Accepts objective / constraint / operator / rhs arguments as Python literals,
runs the two-phase simplex algorithm (via simplex.phases), and writes:
  * the final tableau            (pickle, list[list[float]])
  * the degeneracy report       (pickle, {"degenerate": bool})
  * the problem report          (UTF-8 text, the parsed input LP)
  * the basic-variable report   (pickle, {"basic_variables": dict[str, float]})
  * the pivot log (JSON Lines)  only when --initial_pivots is supplied

If any exception is raised during execution, no output/report/pivot file is
created and the exact Python traceback is printed to stderr.
"""

import argparse
import ast
import json
import pickle
import sys
from decimal import Decimal, ROUND_HALF_UP

from simplex.phases import two_phase_simplex
from simplex.schema import as_float_tableau


# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------

def parse_list_literal(literal):
    """Parse a Python-literal string into a Python value (list/dict/...)."""
    try:
        return ast.literal_eval(literal)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid Python literal: %r" % (literal,)) from exc


def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Two-Phase Simplex Algorithm CLI"
    )
    parser.add_argument(
        "--objective_coeff",
        required=True,
        type=parse_list_literal,
        help="Objective coefficients as a Python literal list of floats",
    )
    parser.add_argument(
        "--constraints_coeff",
        required=True,
        type=parse_list_literal,
        help="Constraint coefficients as a Python literal list of lists",
    )
    parser.add_argument(
        "--constraints_relational_operators",
        required=True,
        type=parse_list_literal,
        help="Constraint relational operators as a Python literal list of strings",
    )
    parser.add_argument(
        "--constraints_rhs",
        required=True,
        type=parse_list_literal,
        help="Constraint right-hand-side values as a Python literal list of floats",
    )
    parser.add_argument(
        "--output_file",
        required=True,
        help="Path to save the final tableau in pickle format",
    )
    parser.add_argument(
        "--degeneracy_report_file",
        required=True,
        help="Path to save the degeneracy report in pickle format",
    )
    parser.add_argument(
        "--problem_report_file",
        required=True,
        help="Path to save the (UTF-8) problem report",
    )
    parser.add_argument(
        "--basic_variables_file",
        required=True,
        help="Path to save the basic-variable report in pickle format",
    )
    parser.add_argument(
        "--initial_pivots",
        type=parse_list_literal,
        default=None,
        help="Optional Python literal list of Phase 1 pivot dicts",
    )
    parser.add_argument(
        "--pivot_log_file",
        default=None,
        help="Path to save the pivot log (JSON Lines); required when --initial_pivots is given",
    )
    return parser


# ----------------------------------------------------------------------------
# Numeric formatting / problem report
# ----------------------------------------------------------------------------

def _round2(value):
    """Round `value` to 2 decimals using standard half-up rounding.

    A result that is zero (including a rounded-away negative) is normalised to a
    positive Decimal('0.00').
    """
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d == 0:
        d = Decimal("0.00")
    return d


def _fmt_num(d):
    """Render a Decimal that has already been rounded to 2 decimals."""
    return str(d)


def _term_string(coeffs, indices):
    """Render a list of (rounded) terms as `<coefficient>*x<index>`.

    Terms are ordered positive (by index), then negative (by index), then zero
    (by index).  The displayed coefficient is always the absolute (positive)
    value; the sign is carried by the leading/separator.  The first term has no
    separator; a leading negative term uses a bare leading ``-`` while later
    negative terms use `` - `` and positive/zero terms use `` + ``.
    """
    pos, neg, zero = [], [], []
    for idx, c in zip(indices, coeffs):
        d = _round2(c)
        if d > 0:
            pos.append((idx, d))
        elif d < 0:
            neg.append((idx, d))
        else:
            zero.append((idx, d))
    pos.sort(key=lambda t: t[0])
    neg.sort(key=lambda t: t[0])
    zero.sort(key=lambda t: t[0])

    # Build the ordered, signed list of (kind, index, abs-value).
    ordered = []
    for idx, d in pos:
        ordered.append(("pos", idx, d))
    for idx, d in neg:
        ordered.append(("neg", idx, abs(d)))
    for idx, d in zero:
        ordered.append(("zero", idx, abs(d)))

    out = []
    first = True
    for kind, idx, val in ordered:
        text = "%s*x%d" % (_fmt_num(val), idx)
        if first:
            prefix = "-" if kind == "neg" else ""
            out.append(prefix + text)
            first = False
        else:
            prefix = " - " if kind == "neg" else " + "
            out.append(prefix + text)
    return "".join(out)


def build_problem_report(objective_coeff, constraints_coeff, ops, rhs):
    """Return the UTF-8 text representation of the parsed input LP."""
    n = len(objective_coeff)
    m = len(ops)
    var_indices = list(range(1, n + 1))

    lines = []
    # Objective line.
    lines.append("maximize " + _term_string(objective_coeff, var_indices))
    # Blank line.
    lines.append("")
    # subject to:
    lines.append("subject to:")
    # One line per input constraint, in input order.
    for i in range(m):
        terms = _term_string(constraints_coeff[i], var_indices)
        rhs_str = _fmt_num(_round2(rhs[i]))
        lines.append("    %s %s %s" % (terms, ops[i], rhs_str))
    # Nonnegativity line.
    nonneg = ", ".join("x%d" % i for i in range(1, n + 1))
    lines.append("    %s >= 0" % nonneg)
    # No trailing newline.
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Solution-derived reports
# ----------------------------------------------------------------------------

def is_degenerate(final_tableau, m):
    """True if any (non-objective) constraint row has an RHS within 1e-9 of 0."""
    for row in range(m):
        if abs(final_tableau[row][-1]) < 1e-9:
            return True
    return False


def basic_variables(final_tableau, n, k, m):
    """Reconstruct the final basis from the final (Phase 2) tableau.

    Columns: Z(0), x1..xn(1..n), s1..sk(n+1..n+k), RHS(last).
    For each constraint row, pick one unit column among the x/s columns; its
    value is the row's RHS.
    """
    basic = {}
    constraint_rows = list(range(m))
    for row in constraint_rows:
        chosen = None
        for col in range(1, n + k + 1):
            if abs(final_tableau[row][col] - 1.0) > 1e-9:
                continue
            is_unit = True
            for other in constraint_rows:
                if other == row:
                    continue
                if abs(final_tableau[other][col]) > 1e-9:
                    is_unit = False
                    break
            if is_unit:
                chosen = col
                break
        if chosen is None:
            # No clean unit column for this row: skip (defensive).
            continue
        if chosen <= n:
            name = "x%d" % chosen
        else:
            name = "s%d" % (chosen - n)
        value = float(final_tableau[row][-1])
        basic[name] = value
    return basic


# ----------------------------------------------------------------------------
# File writing
# ----------------------------------------------------------------------------

def write_pivot_log(pivot_log_file, pivot_log):
    """Write the pivot log as UTF-8 JSON Lines (one object per pivot)."""
    with open(pivot_log_file, "w", encoding="utf-8") as f:
        for entry in pivot_log:
            f.write(json.dumps(entry) + "\n")


def _write_all(
    output_file,
    degeneracy_report_file,
    problem_report_file,
    basic_variables_file,
    pivot_log_file,
    final_tableau,
    pivot_log,
    problem_report,
    degeneracy,
    basic,
):
    # Pickle: final tableau.
    with open(output_file, "wb") as f:
        pickle.dump(as_float_tableau(final_tableau), f)
    # Pickle: degeneracy report.
    with open(degeneracy_report_file, "wb") as f:
        pickle.dump(degeneracy, f)
    # UTF-8 text: problem report.
    with open(problem_report_file, "w", encoding="utf-8") as f:
        f.write(problem_report)
    # Pickle: basic-variable report.
    with open(basic_variables_file, "wb") as f:
        pickle.dump(basic, f)
    # JSON Lines: pivot log (only when requested).
    if pivot_log_file is not None:
        write_pivot_log(pivot_log_file, pivot_log)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.initial_pivots is not None and args.pivot_log_file is None:
        raise ValueError(
            "--pivot_log_file is required when --initial_pivots is provided"
        )

    objective_coeff = args.objective_coeff
    constraints_coeff = args.constraints_coeff
    constraints_relational_operators = args.constraints_relational_operators
    constraints_rhs = args.constraints_rhs
    initial_pivots = args.initial_pivots

    # Run the solver FIRST so that any exception (invalid operator, unbounded,
    # ...) propagates to stderr without creating any output files.
    result = two_phase_simplex(
        objective_coeff,
        constraints_coeff,
        constraints_relational_operators,
        constraints_rhs,
        initial_pivots=initial_pivots,
    )
    final_tableau = result["final_tableau"]
    pivot_log = result["pivot_log"]

    # Compute every report before writing anything, so a failure here (unlikely
    # for valid input) also leaves no files behind.
    n = len(objective_coeff)
    m = len(constraints_relational_operators)
    k = sum(1 for op in constraints_relational_operators if op in ("<=", ">="))

    problem_report = build_problem_report(
        objective_coeff, constraints_coeff, constraints_relational_operators,
        constraints_rhs,
    )
    degeneracy = {"degenerate": is_degenerate(final_tableau, m)}
    basic = {"basic_variables": basic_variables(final_tableau, n, k, m)}

    # All writes after a successful solve.
    _write_all(
        args.output_file,
        args.degeneracy_report_file,
        args.problem_report_file,
        args.basic_variables_file,
        args.pivot_log_file,
        final_tableau,
        pivot_log,
        problem_report,
        degeneracy,
        basic,
    )


if __name__ == "__main__":
    main()
