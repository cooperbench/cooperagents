"""Two-phase simplex orchestration.

Provides two_phase_simplex(...): builds the Phase 1 tableau, optionally applies a
provided prefix of Phase 1 pivots, then either runs a deterministic Bland-rule
path (no --initial_pivots) or computes a minimum-pivot path via BFS
(with --initial_pivots), completing Phase 2 and returning the final tableau
plus the pivot log.
"""

import sys
from collections import deque

from simplex.tableau import (
    build_initial_tableau,
    canonicalize_objective,
    pivot,
    is_optimal,
    entering_columns,
    leaving_rows,
    NEG_TOL,
)
from simplex.schema import as_float_tableau


def _clone(t):
    return [[v for v in row] for row in t]


# ----------------------------------------------------------------------------
# Phase 2 setup
# ----------------------------------------------------------------------------

def _phase2_colmap(n, s_count):
    cm = {}
    cm["Z"] = 0
    for i in range(1, n + 1):
        cm["x%d" % i] = i
    for j in range(1, s_count + 1):
        cm["s%d" % j] = n + j
    cm["RHS"] = n + s_count + 1
    return cm


def _phase2_tableau(p1, objective_coeff, n, s_count, a_count):
    """Drop artificial columns and replace the objective row with the Phase 2
    (original objective) row.  Returns the Phase 2 tableau."""
    # Phase 1 layout: Z(0), x1..xn, s1..sk, a1..ar, RHS
    # Phase 2 layout: Z(0), x1..xn, s1..sk, RHS  (drop a columns)
    keep = [0] + list(range(1, n + 1)) + list(range(n + 1, n + s_count + 1))
    keep.append(len(p1[0]) - 1)  # RHS is last in phase 1
    new_t = []
    for row in p1:
        new_t.append([row[c] for c in keep])
    # Phase 2 objective row: maximize c^T x  ->  Z - c^T x = 0
    obj = [0.0] * len(new_t[0])
    obj[0] = 1.0
    for j in range(1, n + 1):
        obj[j] = -float(objective_coeff[j - 1])
    new_t[-1] = obj
    return new_t


def _phase2_basis(basis_p1, n, s_count, a_count):
    """Map a Phase 1 basis (column indices in Phase 1 layout) to a Phase 2
    basis (column indices in Phase 2 layout).  Artificial basic columns become
    None for their rows."""
    res = []
    for c in basis_p1:
        if c is None:
            res.append(None)
        elif 1 <= c <= n + s_count:
            res.append(c)  # x or s: same index in Phase 2
        else:
            res.append(None)  # artificial or Z/RHS: not basic in Phase 2
    return res


# ----------------------------------------------------------------------------
# Deterministic Bland-rule path
# ----------------------------------------------------------------------------

def _bland_step(tableau, basis):
    """Return (enter_col, leave_row) via Bland's rule, or (None, None) if
    optimal.  Raises RuntimeError if unbounded (entering col with no positive
    pivot-col entry)."""
    cols = entering_columns(tableau)
    if not cols:
        return None, None
    col = min(cols)  # Bland: smallest index
    rows = leaving_rows(tableau, col)
    if not rows:
        raise Exception("Unbounded problem")  # standard built-in Exception
    row = min(rows)  # Bland: smallest row index
    return col, row


def _run_phase_bland(tableau, basis):
    """Run a single phase to optimality using Bland's rule.
    Returns the final tableau.  Raises Exception if unbounded."""
    while True:
        col, row = _bland_step(tableau, basis)
        if col is None:
            break
        pivot(tableau, row, col)
        leaving = basis[row]
        basis[row] = col
    return tableau


# ----------------------------------------------------------------------------
# Minimum-pivot BFS (Phase 1 remaining + Phase 2)
# ----------------------------------------------------------------------------

def _bfs_min_pivot(start_tableau, start_basis, objective_coeff, n, s_count, a_count):
    """Breadth-first search over valid pivots to find a minimum-pivot path to a
    Phase 2 optimal tableau.  The state is (phase, basis_tuple).  Returns a
    dict with: path (list of (phase, enter_col, leave_row)), final (tableau or
    None), status ('optimal'/'infeasible'/'unbounded')."""
    # We store for each state the actual tableau (for the pivot log).
    # Start: Phase 1.
    start_basis_t = tuple(start_basis)
    start_key = (1, start_basis_t)
    actual = {start_key: (start_tableau, start_basis)}
    parents = {start_key: (None, None)}  # (prev_key, pivot_info_or_None)
    visited = {start_key}

    dq = deque([start_key])
    goal_key = None
    best_effort_key = None  # phase 1 optimal but infeasible (or unbounded last)

    # track last phase-1 state in case of unbounded (no phase-1-optimal reached)
    while dq:
        key = dq.popleft()
        phase, basis_t = key
        t = actual[key][0]

        if phase == 1:
            if is_optimal(t):
                if t[-1][-1] >= -1e-7:  # feasible
                    # transition to Phase 2 (cost 0)
                    p2 = _phase2_tableau(t, objective_coeff, n, s_count, a_count)
                    b2 = _phase2_basis(basis_t, n, s_count, a_count)
                    canonicalize_objective(p2, b2)
                    p2_key = (2, tuple(b2))
                    if p2_key not in visited:
                        visited.add(p2_key)
                        parents[p2_key] = (key, ("transition", None))
                        actual[p2_key] = (p2, b2)
                        dq.append(p2_key)
                else:
                    # infeasible
                    if best_effort_key is None:
                        best_effort_key = key
                continue
            # not optimal: generate pivots
            for col in entering_columns(t):
                rows = leaving_rows(t, col)
                if not rows:
                    continue  # unbounded direction; skip
                for row in rows:
                    nt = _clone(t)
                    pivot(nt, row, col)
                    nb = list(basis_t)
                    nb[row] = col
                    nb_t = tuple(nb)
                    nk = (1, nb_t)
                    if nk in visited:
                        continue
                    visited.add(nk)
                    parents[nk] = (key, (1, col, row))
                    actual[nk] = (nt, nb)
                    dq.append(nk)
        else:  # phase == 2
            if is_optimal(t):
                goal_key = key
                continue
            for col in entering_columns(t):
                rows = leaving_rows(t, col)
                if not rows:
                    continue
                for row in rows:
                    nt = _clone(t)
                    pivot(nt, row, col)
                    nb = list(basis_t)
                    nb[row] = col
                    nb_t = tuple(nb)
                    nk = (2, nb_t)
                    if nk in visited:
                        continue
                    visited.add(nk)
                    parents[nk] = (key, (2, col, row))
                    actual[nk] = (nt, nb)
                    dq.append(nk)

    if goal_key is not None:
        path = _reconstruct_path(parents, goal_key)
        return {"path": path, "final": actual[goal_key][0], "status": "optimal"}

    if best_effort_key is not None:
        path = _reconstruct_path(parents, best_effort_key)
        return {"path": path, "final": None, "phase1_final": actual[best_effort_key][0],
                "status": "infeasible"}

    # no phase-1-optimal reached: unbounded
    return {"path": [], "final": None, "phase1_final": start_tableau, "status": "unbounded"}


def _reconstruct_path(parents, key):
    path = []
    k = key
    while parents[k][0] is not None:
        pk, info = parents[k]
        if info is not None:
            path.append(info)
        k = pk
    path.reverse()
    return path


# ----------------------------------------------------------------------------
# Public entry
# ----------------------------------------------------------------------------

def two_phase_simplex(objective_coeff, constraints_coeff, constraints_relational_operators,
                      constraints_rhs, initial_pivots=None):
    """Run the two-phase simplex algorithm.

    Returns a dict with:
        'final_tableau': the final (Phase 2 optimal, or best-effort) tableau
        'pivot_log': list of dicts (one per pivot) with keys
            step, phase, enter_column, leave_row, tableau
    Raises Exception if the problem is unbounded.
    """
    from simplex.tableau import build_initial_tableau as _b

    tableau, basis, s_count, a_count, colmap = _b(
        objective_coeff, constraints_coeff, constraints_relational_operators, constraints_rhs)
    n = len(objective_coeff)
    # name of each column (Phase 1)
    name_of = {}
    for name, idx in colmap.items():
        name_of[idx] = name
    # canonicalize the Phase 1 objective row w.r.t. the initial basis
    canonicalize_objective(tableau, basis)

    pivot_log = []
    step = 0

    if initial_pivots is None:
        # Deterministic Bland-rule path through both phases.
        # Phase 1
        _run_phase_bland(tableau, basis)
        if tableau[-1][-1] < -1e-7:
            # infeasible: best-effort is the current (Phase 1) tableau
            return {"final_tableau": as_float_tableau(tableau), "pivot_log": pivot_log}
        # Phase 2
        p2 = _phase2_tableau(tableau, objective_coeff, n, s_count, a_count)
        b2 = _phase2_basis(basis, n, s_count, a_count)
        canonicalize_objective(p2, b2)
        # run phase 2 with Bland's rule (raise on unbounded)
        while True:
            col, row = _bland_step(p2, b2)
            if col is None:
                break
            pivot(p2, row, col)
            leaving = b2[row]
            b2[row] = col
        return {"final_tableau": as_float_tableau(p2), "pivot_log": pivot_log}
    else:
        # provided prefix of Phase 1 pivots, then minimum-pivot continuation.
        # 1. apply the provided prefix (Phase 1 only), logging each pivot.
        for pv in initial_pivots:
            enter_name = pv["enter_column"]
            leave_label = pv["leave_row"]
            col = colmap[enter_name]
            row = int(leave_label[1:])  # R<i> -> i
            # pivot
            pivot(tableau, row, col)
            leaving = basis[row]
            basis[row] = col
            step += 1
            log_tableau = _clone(tableau)
            pivot_log.append({
                "step": step,
                "phase": 1,
                "enter_column": enter_name,
                "leave_row": leave_label,
                "tableau": log_tableau,
            })

        # 2. minimum-pivot continuation via BFS.
        res = _bfs_min_pivot(tableau, basis, objective_coeff, n, s_count, a_count)

        if res["status"] == "unbounded":
            raise Exception("Unbounded problem")

        if res["status"] == "infeasible":
            # apply logged prefix already done; append no more pivots.
            final = res.get("phase1_final")
            if final is None:
                final = _clone(tableau)
            return {"final_tableau": as_float_tableau(final), "pivot_log": pivot_log}

        # optimal: append the BFS continuation pivots (Phase 1 then Phase 2).
        # We need to replay the BFS path to produce the tableaus for the log.
        # The BFS 'path' gives (phase, enter_col, leave_row) for each pivot;
        # but the Phase 1->Phase 2 transition is a non-pivot (not logged).
        # Replay from the current (post-prefix) state.
        cur_tableau = _clone(tableau)
        cur_basis = list(basis)
        cur_phase = 1
        for info in res["path"]:
            if info[0] == "transition":
                # Phase 1 -> Phase 2 (non-pivot): rebuild state
                cur_tableau = _phase2_tableau(cur_tableau, objective_coeff, n, s_count, a_count)
                cur_basis = _phase2_basis(cur_basis, n, s_count, a_count)
                canonicalize_objective(cur_tableau, cur_basis)
                cur_phase = 2
                continue
            ph, col, row = info
            # the col is a Phase 1 or Phase 2 column index; apply pivot
            assert cur_phase == ph
            pivot(cur_tableau, row, col)
            leaving = cur_basis[row]
            cur_basis[row] = col
            step += 1
            log_tableau = _clone(cur_tableau)
            pivot_log.append({
                "step": step,
                "phase": ph,
                "enter_column": name_of.get(col, _phase2_name(col, n, s_count, a_count)),
                "leave_row": "R%d" % row,
                "tableau": log_tableau,
            })

        final = res["final"]
        return {"final_tableau": as_float_tableau(final), "pivot_log": pivot_log}


def _phase2_name(col, n, s_count, a_count):
    if col == 0:
        return "Z"
    if 1 <= col <= n:
        return "x%d" % col
    if n + 1 <= col <= n + s_count:
        return "s%d" % (col - n)
    return "?%d" % col
