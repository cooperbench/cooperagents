"""Two-phase simplex engine with minimum-pivot search.

Column layout
-------------
Phase 1: [Z, x1..xn, s1..sk, a1..ar, RHS]
Phase 2 / final: [Z, x1..xn, s1..sk, RHS]

- k = number of '<=' and '>=' constraints (slack/surplus columns).
- r = number of '>=' and '=' constraints (artificial columns).
- A '<=' constraint adds a slack column (coefficient +1).
- A '>=' constraint adds a surplus column (coefficient -1) and an artificial column (+1).
- An '=' constraint adds only an artificial column (+1).

The problem is a maximization problem.  The Phase 1 objective minimizes the
sum of the artificial variables (its bottom row stores +1 for each
artificial column, so the objective value is -RHS of the bottom row).
The Phase 2 objective row stores -c_j for the decision-variable columns, so
a negative entry indicates that the variable can still improve the
objective.
"""

import copy

EPS = 1e-9


def validate_operators(operators):
    allowed = {"<=", ">=", "="}
    for op in operators:
        if op not in allowed:
            raise ValueError(
                "Invalid relational operator %r; must be one of %s"
                % (op, sorted(allowed))
            )


def _snap(x):
    # Snap only floating-point noise that is essentially zero.  We do NOT
    # round to a fixed decimal precision, because intermediate pivots can
    # legitimately contain non-terminating decimals (e.g. 1/3) and rounding
    # those to a fixed precision loses precision that later pivots amplify
    # into visible residue in the final tableau.  Instead we only flush
    # values whose magnitude is within the solver's tolerance to exactly 0,
    # which keeps the final tableau exact for well-conditioned problems
    # while removing pure numerical noise.
    if abs(x) < 1e-9:
        return 0.0
    return x


def clean(t):
    return [[_snap(v) for v in row] for row in t]


class SimplexEngine:
    def __init__(self, objective, constraints, operators, rhs, initial_pivots=None):
        self.n = len(objective)
        self.m = len(constraints)
        self.objective = [float(c) for c in objective]
        self.constraints = [[float(c) for c in row] for row in constraints]
        self.operators = list(operators)
        self.rhs = [float(b) for b in rhs]
        self.initial_pivots = initial_pivots or []
        self.validate = True

        # k = number of slack/surplus columns (<= and >= constraints).
        # r = number of artificial columns (>= and = constraints).
        self.k = sum(1 for op in operators if op in ("<=", ">="))
        self.r = sum(1 for op in operators if op in (">=", "="))

        # Per-constraint column assignment.
        self.s_idx = [None] * self.m
        self.a_idx = [None] * self.m
        s_counter = 0
        a_counter = 0
        base = 1 + self.n  # first slack column index
        for i in range(self.m):
            op = self.operators[i]
            if op in ("<=", ">="):
                self.s_idx[i] = base + s_counter
                s_counter += 1
            if op in (">=", "="):
                self.a_idx[i] = base + self.k + a_counter
                a_counter += 1

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------
    def ncols1(self):
        return 1 + self.n + self.k + self.r + 1

    def ncols2(self):
        return 1 + self.n + self.k + 1

    def art_col_indices(self):
        return [idx for idx in self.a_idx if idx is not None]

    def _col_name(self, j, phase):
        n, k = self.n, self.k
        if j == 0:
            return "Z"
        if 1 <= j <= n:
            return "x%d" % j
        if 1 + n <= j < 1 + n + k:
            return "s%d" % (j - n)
        if phase == 1 and 1 + n + k <= j < 1 + n + k + self.r:
            return "a%d" % (j - n - k)
        return None

    def _name_to_col(self, name, phase):
        n, k = self.n, self.k
        if name == "Z":
            return 0
        if name.startswith("x"):
            return int(name[1:])
        if name.startswith("s"):
            return 1 + n + (int(name[1:]) - 1)
        if name.startswith("a") and phase == 1:
            return 1 + n + k + (int(name[1:]) - 1)
        raise ValueError("Unknown column name %r" % name)

    # ------------------------------------------------------------------
    # Tableau construction
    # ------------------------------------------------------------------
    def build_phase1(self):
        n, m, k, r = self.n, self.m, self.k, self.r
        ncols = self.ncols1()
        tableau = []
        for i in range(m):
            row = [0.0] * ncols
            for j in range(n):
                row[1 + j] = float(self.constraints[i][j])
            if self.s_idx[i] is not None:
                if self.operators[i] == "<=":
                    row[self.s_idx[i]] = 1.0
                elif self.operators[i] == ">=":
                    row[self.s_idx[i]] = -1.0
            if self.a_idx[i] is not None:
                row[self.a_idx[i]] = 1.0
            row[-1] = float(self.rhs[i])
            tableau.append(row)

        obj = [0.0] * ncols
        obj[0] = 1.0
        for i in range(m):
            if self.a_idx[i] is not None:
                obj[self.a_idx[i]] = 1.0
        tableau.append(obj)

        # Canonicalize the objective row using the initial basic columns.
        for i in range(m):
            if self.operators[i] == "<=":
                bc = self.s_idx[i]
            else:
                bc = self.a_idx[i]
            factor = tableau[-1][bc]
            if abs(factor) > EPS:
                for j in range(ncols):
                    tableau[-1][j] -= factor * tableau[i][j]

        return clean(tableau)

    # ------------------------------------------------------------------
    # Pivot operations
    # ------------------------------------------------------------------
    def apply_pivot(self, tableau, enter, leave):
        t = [row[:] for row in tableau]
        pv = t[leave][enter]
        if abs(pv) < EPS:
            raise ValueError("Pivot element is zero")
        newrow = [v / pv for v in t[leave]]
        t[leave] = newrow
        for i in range(len(t)):
            if i != leave:
                f = t[i][enter]
                if abs(f) > EPS:
                    for j in range(len(newrow)):
                        t[i][j] -= f * newrow[j]
        return clean(t)

    def is_optimal(self, tableau):
        return all(tableau[-1][j] >= -EPS for j in range(1, len(tableau[0]) - 1))

    def phase1_objective_value(self, tableau):
        return -tableau[-1][-1]

    # ------------------------------------------------------------------
    # Basis / column name extraction
    # ------------------------------------------------------------------
    def _basis(self, tableau, phase):
        n, m = self.n, self.m
        ncols = len(tableau[0])
        basis = []
        for i in range(m):
            found = None
            for j in range(1, ncols - 1):
                if abs(tableau[i][j] - 1.0) < EPS and all(
                    abs(tableau[kk][j]) < EPS for kk in range(m) if kk != i
                ):
                    found = self._col_name(j, phase)
                    break
            basis.append(found if found else None)
        return tuple(basis)

    def _valid_pivots(self, tableau):
        """Return (valid_pivots, unbounded)."""
        ncols = len(tableau[0])
        obj = tableau[-1]
        unbounded = False
        results = []
        for j in range(1, ncols - 1):
            if obj[j] < -EPS:
                best_ratio = float("inf")
                rows = []
                has_positive = False
                for i in range(len(tableau) - 1):
                    val = tableau[i][j]
                    if val > EPS:
                        has_positive = True
                        ratio = tableau[i][-1] / val
                        if ratio < best_ratio - EPS:
                            best_ratio = ratio
                            rows = [i]
                        elif abs(ratio - best_ratio) <= EPS:
                            rows.append(i)
                if not has_positive:
                    unbounded = True
                    continue
                for i in rows:
                    results.append((j, i))
        return results, unbounded

    # ------------------------------------------------------------------
    # Phase 1 -> Phase 2 transition
    # ------------------------------------------------------------------
    def _to_phase2(self, tableau):
        art_cols = set(self.art_col_indices())
        t2 = []
        for row in tableau:
            newrow = [row[j] for j in range(len(row)) if j not in art_cols]
            t2.append(newrow)
        ncols2 = self.ncols2()
        t2[-1] = [0.0] * ncols2
        t2[-1][0] = 1.0
        for j in range(self.n):
            t2[-1][1 + j] = -float(self.objective[j])
        # Canonicalize using current basic columns.
        basic_cols = []
        m = self.m
        for i in range(m):
            found = None
            for j in range(1, ncols2 - 1):
                if abs(t2[i][j] - 1.0) < EPS and all(
                    abs(t2[kk][j]) < EPS for kk in range(m) if kk != i
                ):
                    found = j
                    break
            basic_cols.append(found)
        for i in range(m):
            bc = basic_cols[i]
            if bc is not None:
                factor = t2[-1][bc]
                if abs(factor) > EPS:
                    for j in range(ncols2):
                        t2[-1][j] -= factor * t2[i][j]
        return clean(t2)

    # ------------------------------------------------------------------
    # Apply provided prefix pivots (Phase 1 only).
    # ------------------------------------------------------------------
    def _apply_prefix(self, tableau):
        log = []
        for piv in self.initial_pivots:
            ec = self._name_to_col(piv["enter_column"], 1)
            lr = int(piv["leave_row"][1:])
            tableau = self.apply_pivot(tableau, ec, lr)
            log.append(
                (1, piv["enter_column"], piv["leave_row"], copy.deepcopy(tableau))
            )
        return tableau, log

    # ------------------------------------------------------------------
    # Main solve.
    # ------------------------------------------------------------------
    def solve(self):
        validate_operators(self.operators)
        tableau, prefix_log = self._apply_prefix(self.build_phase1())
        start_basis = self._basis(tableau, 1)
        start_key = (1, start_basis)

        import heapq

        INF = float("inf")
        dist = {}
        prev = {}
        prev_pivot = {}
        tableaus = {}
        dist[start_key] = 0
        tableaus[start_key] = copy.deepcopy(tableau)
        pq = []
        _seq = [0]
        def _push(d, phase, basis):
            _seq[0] += 1
            heapq.heappush(pq, (d, _seq[0], phase, basis))
        _push(0, 1, start_basis)
        goal_key = None
        unbounded_hit = False
        infeasible_states = []  # keys of feasible-check phase1 optimal with W>0
        counter = 0
        CAP = 300000

        while pq:
            d, _, phase, basis = heapq.heappop(pq)
            key = (phase, basis)
            if d > dist.get(key, INF):
                continue
            t = tableaus.get(key)
            if t is None:
                continue
            counter += 1
            if counter > CAP:
                break
            valid, unbounded = self._valid_pivots(t)
            if phase == 2 and unbounded:
                unbounded_hit = True

            if phase == 1:
                if self.is_optimal(t):
                    w = self.phase1_objective_value(t)
                    if w > EPS:
                        infeasible_states.append(key)
                        continue
                    t2 = self._to_phase2(t)
                    b2 = self._basis(t2, 2)
                    k2 = (2, b2)
                    if d < dist.get(k2, INF):
                        dist[k2] = d
                        prev[k2] = key
                        tableaus[k2] = t2
                        _push(d, 2, b2)
                else:
                    for (enter, leave) in valid:
                        t2 = self.apply_pivot(t, enter, leave)
                        b2 = self._basis(t2, 1)
                        k2 = (1, b2)
                        nd = d + 1
                        if nd < dist.get(k2, INF):
                            dist[k2] = nd
                            prev[k2] = key
                            prev_pivot[k2] = (enter, leave)
                            tableaus[k2] = t2
                            _push(nd, 1, b2)
            else:  # phase == 2
                if self.is_optimal(t):
                    if goal_key is None or d < dist.get(goal_key, INF):
                        goal_key = key
                else:
                    for (enter, leave) in valid:
                        t2 = self.apply_pivot(t, enter, leave)
                        b2 = self._basis(t2, 2)
                        k2 = (2, b2)
                        nd = d + 1
                        if nd < dist.get(k2, INF):
                            dist[k2] = nd
                            prev[k2] = key
                            prev_pivot[k2] = (enter, leave)
                            tableaus[k2] = t2
                            _push(nd, 2, b2)

        # Decide the outcome.
        if goal_key is not None:
            log = self._reconstruct(prefix_log, start_key, goal_key, tableaus, prev, prev_pivot)
            final = copy.deepcopy(tableaus[goal_key])
            return {"final": final, "log": log, "feasible": True, "unbounded": False}

        if unbounded_hit:
            raise Exception("Unbounded")

        if infeasible_states:
            best = self._best_infeasible(prefix_log, start_key, infeasible_states, tableaus, prev, prev_pivot)
            return {"final": best["final"], "log": best["log"], "feasible": False, "unbounded": False}

        # Exhausted without a goal: greedy fallback (handles large problems).
        return self._greedy_fallback(tableau, prefix_log, unbounded_hit)

    # ------------------------------------------------------------------
    def _reconstruct(self, prefix_log, start_key, goal_key, tableaus, prev, prev_pivot):
        suffix = []
        cur = goal_key
        while cur in prev:
            piv = prev_pivot.get(cur)
            p = prev[cur]
            if piv is not None:
                enter, leave = piv
                enter_name = self._col_name(enter, cur[0])
                leave_label = "R%d" % leave
                post = copy.deepcopy(tableaus[cur])
                suffix.append((cur[0], enter_name, leave_label, post))
            cur = p
        suffix.reverse()
        return list(prefix_log) + suffix

    def _best_infeasible(self, prefix_log, start_key, infeasible_states, tableaus, prev, prev_pivot):
        # pick the infeasible phase-1 optimal state with the smallest distance
        best_key = None
        best_d = float("inf")
        for key in infeasible_states:
            d = dist_to(start_key, key, prev)
            if d < best_d:
                best_d = d
                best_key = key
        final = copy.deepcopy(tableaus[best_key])
        log = self._reconstruct(prefix_log, start_key, best_key, tableaus, prev, prev_pivot)
        return {"final": final, "log": log, "feasible": False, "unbounded": False}

    def _greedy_fallback(self, start_tableau, prefix_log, unbounded_hit):
        import copy as _c

        t = _c.deepcopy(start_tableau)
        log = list(prefix_log)
        # phase 1 (Bland's rule for anti-cycling)
        while not self.is_optimal(t):
            valid, unbounded = self._valid_pivots(t)
            if not valid:
                break
            enter, leave = _bland(valid)
            t = self.apply_pivot(t, enter, leave)
            log.append((1, self._col_name(enter, 1), "R%d" % leave, _c.deepcopy(t)))
        w = self.phase1_objective_value(t)
        if w > EPS:
            return {"final": t, "log": log, "feasible": False, "unbounded": False}
        # phase 2
        t = self._to_phase2(t)
        while not self.is_optimal(t):
            valid, unbounded = self._valid_pivots(t)
            if unbounded and not valid:
                raise Exception("Unbounded")
            if not valid:
                break
            enter, leave = _bland(valid)
            t = self.apply_pivot(t, enter, leave)
            log.append((2, self._col_name(enter, 2), "R%d" % leave, _c.deepcopy(t)))
        return {"final": t, "log": log, "feasible": True, "unbounded": unbounded}


def _bland(valid):
    # Bland's rule: smallest index entering column, then smallest row.
    enter = min(j for (j, i) in valid)
    rows = [i for (j, i) in valid if j == enter]
    leave = min(rows)
    return enter, leave


def dist_to(start_key, key, prev):
    d = 0
    cur = key
    while cur != start_key and cur in prev:
        cur = prev[cur]
        d += 1
    return d
