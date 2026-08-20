"""Two-phase simplex tableau construction and pivot operations.

Column layout (fixed order):
    index 0          -> Z
    1 .. n           -> x1 .. xn   (decision variables)
    n+1 .. n+k       -> s1 .. sk   (slack/surplus, one per '<=' or '>=' constraint)
    n+k+1 .. n+k+r   -> a1 .. ar   (artificial, one per '>=' or '=' constraint) [Phase 1 only]
    last             -> RHS

Row layout:
    rows 0 .. m-1    -> original constraint rows (labels R0 .. R<m-1>)
    row m            -> objective row (never a leaving row)
"""

from copy import deepcopy

NEG_TOL = 1e-9
ZERO_TOL = 1e-9


def validate_operators(ops):
    for op in ops:
        if op not in ("<=", ">=", "="):
            raise ValueError("Invalid relational operator: %r" % (op,))


def build_initial_tableau(objective_coeff, constraints_coeff, ops, rhs):
    """Build the Phase 1 tableau (objective row NOT yet canonicalized).

    Returns:
        tableau: list[list[float]]  (m constraint rows + 1 objective row)
        basis: list[int]           basic variable column index per constraint row
        s_count: int               number of slack/surplus variables
        a_count: int              number of artificial variables
        colmap: dict[str,int]    column name -> index
    """
    validate_operators(ops)
    n = len(objective_coeff)
    m = len(ops)
    s_count = sum(1 for op in ops if op in ("<=", ">="))
    a_count = sum(1 for op in ops if op in (">=", "="))

    total_cols = 1 + n + s_count + a_count + 1  # Z + x + s + a + RHS
    colmap = {}
    colmap["Z"] = 0
    for i in range(1, n + 1):
        colmap["x%d" % i] = i
    for j in range(1, s_count + 1):
        colmap["s%d" % j] = n + j
    for j in range(1, a_count + 1):
        colmap["a%d" % j] = n + s_count + j
    colmap["RHS"] = total_cols - 1

    s_next = 1
    a_next = 1
    tableau = []
    basis = []
    for i in range(m):
        row = [0.0] * total_cols
        row[colmap["Z"]] = 0.0
        for j in range(n):
            row[colmap["x%d" % (j + 1)]] = float(constraints_coeff[i][j])
        op = ops[i]
        if op == "<=":
            row[colmap["s%d" % s_next]] = 1.0
            basis.append(colmap["s%d" % s_next])
            s_next += 1
        elif op == ">=":
            row[colmap["s%d" % s_next]] = -1.0
            basis.append(colmap["a%d" % a_next])
            row[colmap["a%d" % a_next]] = 1.0
            s_next += 1
            a_next += 1
        elif op == "=":
            basis.append(colmap["a%d" % a_next])
            row[colmap["a%d" % a_next]] = 1.0
            a_next += 1
        row[colmap["RHS"]] = float(rhs[i])
        tableau.append(row)

    # Phase 1 objective row: maximize -w = -sum(a_j).  Row is Z - c^T x = 0,
    # so coefficient of a_j is -(-1) = +1.
    obj = [0.0] * total_cols
    obj[colmap["Z"]] = 1.0
    for j in range(1, a_count + 1):
        obj[colmap["a%d" % j]] = 1.0
    obj[colmap["RHS"]] = 0.0
    tableau.append(obj)

    return tableau, basis, s_count, a_count, colmap


def canonicalize_objective(tableau, basis):
    """Zero out the objective row at every basic column (make it consistent
    with the current basis).  Modifies tableau in place."""
    obj = tableau[-1]
    m = len(tableau) - 1
    for i in range(m):
        c = basis[i]
        if c is None:
            continue
        val = obj[c]
        if abs(val) > 1e-12:
            row = tableau[i]
            for k in range(len(obj)):
                obj[k] = obj[k] - val * row[k]


def pivot(tableau, row, col):
    """Perform the simplex pivot with pivot element at (row, col).
    Normalizes the pivot row and eliminates the pivot column from all other
    rows.  Modifies tableau in place.  Returns a NEW list (rows are shared).
    """
    pv = tableau[row][col]
    prow = tableau[row]
    # normalize pivot row
    for k in range(len(prow)):
        prow[k] = prow[k] / pv
    # eliminate pivot column from other rows
    for i in range(len(tableau)):
        if i == row:
            continue
        factor = tableau[i][col]
        if abs(factor) < 1e-15:
            continue
        r = tableau[i]
        for k in range(len(r)):
            r[k] = r[k] - factor * prow[k]
    return tableau


def is_optimal(tableau):
    """Objective row is optimal if no variable column has a value < -tol.
    (Z and RHS are excluded.)"""
    obj = tableau[-1]
    for k in range(1, len(obj) - 1):
        if obj[k] < -NEG_TOL:
            return False
    return True


def entering_columns(tableau):
    """Return list of column indices whose objective-row value is < -tol
    (candidates for entering)."""
    obj = tableau[-1]
    res = []
    for k in range(1, len(obj) - 1):
        if obj[k] < -NEG_TOL:
            res.append(k)
    return res


def leaving_rows(tableau, col):
    """Return the list of constraint-row indices that attain the minimum ratio
    RHS / pivot-col-value among rows with a positive pivot-col value.
    Returns empty list if there is no positive pivot-col value (unbounded)."""
    rows = []
    best = None
    for i in range(len(tableau) - 1):
        v = tableau[i][col]
        if v > NEG_TOL:
            rhs = tableau[i][-1]
            ratio = rhs / v
            if best is None or ratio < best - 1e-9:
                best = ratio
                rows = [i]
            elif abs(ratio - best) <= 1e-9:
                rows.append(i)
    return rows

def phase1_optimal_value(tableau):
    """Phase 1 optimal value -w = (objective row RHS). Returns the float value.
    Feasible iff this value is >= -tol (i.e. w <= tol)."""
    return tableau[-1][-1]
