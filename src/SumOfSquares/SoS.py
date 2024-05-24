from __future__ import annotations # for type hinting

import math
import picos as pic
import sympy as sp
import numpy as np
from picos import Problem
from collections import defaultdict
from operator import floordiv, and_
from itertools import combinations
from typing import List, Optional

from .util import *
from .basis import Basis, poly_variable

class SOSProblem(Problem):
    '''Defines an Sum of Squares problem, a subclass of picos.Problem.
    (also see: https://gitlab.com/picos-api/picos/-/issues/138 for an
    implementation that also extends picos.Problem)
    '''
    def __init__(self, *args, **kwargs):
        '''Takes same arguments as picos.Problem
        '''
        Problem.__init__(self, *args, **kwargs)
        self._sym_var_map = {}
        self._sos_constraints = {}
        self._sos_const_count = 0
        self._aux_var_count = 0
        self._pexpect_count = 0

    def __getitem__(self, sym: sp.Symbol) -> pic.RealVariable:
        assert isinstance(sym, sp.Symbol), f'{sym} must be a sympy symbol!'
        return self.sym_to_var(sym)

    def sym_to_var(self, sym: sp.Symbol) -> pic.RealVariable:
        '''Map between a sympy symbol to a unique picos variable. As sympy
        symbols are hashable, each symbol is assigned a unique picos variable.
        A new picos variable is created if it previously doesn't exist.
        '''
        if sym not in self._sym_var_map:
            self._sym_var_map[sym] = pic.RealVariable(repr(sym))
        return self._sym_var_map[sym]

    def subs_with_sol(self, expr: sp.Expr) -> sp.Expr:
        ''' Substitute symbols in sympy expression with values obtained from
        solving the problem'''
        syms_with_values = [(sym, var.value)
                            for sym, var in self._sym_var_map.items()
                            if var.value is not None]
        return expr.subs(syms_with_values)

    def sp_to_picos(self, expr: sp.Expr) -> pic.expressions.Expression:
        '''Converts a sympy affine expression to a picos expression, converting
        numeric values to floats, and sympy symbols to picos variables.
        '''
        if expr.func == sp.Symbol:
            return self.sym_to_var(expr)
        elif expr.func == sp.Add:
            return sum(map(self.sp_to_picos, expr.args))
        elif expr.func == sp.Mul:
            return prod(map(self.sp_to_picos, expr.args))
        else:
            return pic.Constant(float(expr))

    def sp_mat_to_picos(self, mat: sp.Matrix) -> pic.expressions.Expression:
        '''Converts a sympy matrix a picos affine expression, converting
        numeric values to floats, and sympy symbols to picos variables.
        '''
        num_rows, num_cols = mat.shape
        # Use picos operator overloading
        return reduce(floordiv, [reduce(and_, map(self.sp_to_picos, mat.row(r)))
                                 for r in range(num_rows)])

    def add_sos_constraint(self, expr: sp.Expr, variables: List[sp.Symbol],
                           name: str='', sparse: bool=False) -> SOSConstraint:
        '''Adds a constraint that the polynomial EXPR is a Sum-of-Squares. EXPR
        is a sympy expression treated as a polynomial in VARIABLES. Any symbols
        in EXPR not in VARIABLES are converted to picos variables
        (see SOSProblem.sym_to_var). Can optionally name the constraint with
        NAME. SPARSE uses Newton polytope reduction to do computations in a
        reduced-size basis. Returns a SOSConstraint object.
        '''
        self._sos_const_count += 1
        name = name or f'_Q{self._sos_const_count}'
        variables = sorted(variables, key=str) # To lex order
        poly = sp.poly(expr, variables)
        deg = poly.total_degree()
        assert deg % 2 == 0, 'Polynomial degree must be even!'

        hom = is_hom(poly, deg)
        mono_to_coeffs = dict(zip(poly.monoms(), map(self.sp_to_picos, poly.coeffs())))
        basis = Basis.from_poly_lex(poly, sparse=sparse)

        Q = pic.SymmetricVariable(name, len(basis))
        for mono, pairs in basis.sos_sym_entries.items():
            coeff = mono_to_coeffs.get(mono, 0)
            self.add_constraint(sum(Q[i,j] for i,j in pairs) == coeff)

        pic_const = self.add_constraint(Q >> 0)
        return SOSConstraint(pic_const, Q, basis, variables, deg)

    def add_matrix_sos_constraint(self, mat: sp.Matrix, variables: List[sp.Symbol],
                                  name: str='', sparse: bool=False, aux_var_name: str=''
                                  ) -> SOSConstraint:
        '''Adds a constraint that MAT is sum of squares. This is done by
        defining a polynomial (using auxillary variables) that is sum of squares
        iff MAT is a sum of squares matrix.
        '''
        n, m = mat.shape
        assert n == m, 'Matrix must be square!'

        self._aux_var_count += 1
        aux_var_name = aux_var_name or f'_y{self._aux_var_count}'
        aux_vars = list(sp.symbols(f'{aux_var_name}_:{n}'))

        y = sp.Matrix([aux_vars])
        p = (y @ mat @ y.T)[0] # p is sos iff mat is a sos matrix
        return self.add_sos_constraint(p, aux_vars + variables, name=name, sparse=sparse)

    def get_pexpect(self, variables: List[sp.Symbol], deg:int,
                    hom: bool=False, name: str=''
                    ) -> Callable[[sp.Expr], pic.expressions.Expression]:
        '''Returns a degree DEG pseudoexpectation operator. This operator is a
        function that takes in a polynomial of at most degree DEG in VARIABLES,
        and returns a picos affine expression. If HOM=True, this polynomial must
        also be homogeneous. This operator has the property that
        pexpect(p(x)^2) >= 0 for any suitable polynomial p(x).

        Since the return value of pexpect(p(x)) is a picos expression, it can be
        used in other constraints/objectives in the current problem.

        The constraints associated with this operator are registered with the
        SOSProblem instance, and can be optionally named as NAME.

        '''
        self._pexpect_count += 1
        name = name or f'_X{self._pexpect_count}'
        variables = sorted(variables, key=str) # To lex order
        basis = Basis.from_degree(len(variables), deg//2)

        X = pic.SymmetricVariable(name, len(basis))
        for monom, indices in basis.sos_sym_entries.items():
            if len(indices) > 1:
                ip, jp = indices[0]
                for i,j in indices[1:]:
                    self.add_constraint(X[ip, jp] == X[i, j])
                    ip, jp = i, j

        self.add_constraint(X >> 0)

        def pexpect(p):
            poly = sp.poly(p, variables)
            basis.check_can_represent(poly)
            return self.sp_mat_to_picos(basis.sos_sym_poly_repr(poly)) | X
        return pexpect

def poly_opt_prob(vars       : List[sp.Symbol],
                  obj        : sp.Expr,
                  eqs        : Optional[List[sp.Expr]] = None,
                  ineqs      : Optional[List[sp.Expr]] = None,
                  ineq_prods : bool = False,
                  deg        : Optional[int] = None,
                  sparse     : bool = False) -> SOSProblem:
    '''Formulates and returns a degree DEG Sum-of-Squares relaxation of a polynomial
    optimization problem in variables VARS that mininizes OBJ subject to
    equality constraints EQS (g(x) = 0) and inequality constraints INEQS (h(x)
    >= 0). INEQ_PRODS determines if products of inequalities are used. SPARSE
    uses Newton polytope reduction to do computations in a reduced-size
    basis. Returns an instance of SOSProblem.

    '''
    prob = SOSProblem()
    gamma = sp.symbols('gamma')
    gamma_p = prob.sym_to_var(gamma)
    eqs, ineqs = eqs or [], ineqs or []
    deg = get_poly_degree(vars, [obj] + eqs + ineqs, deg=deg)

    f = 0 # obviously non-negative polynomial for (in)equalities constraints
    for i, eq in enumerate(eqs):
        p = poly_variable(f'c{i}', vars, 2*deg - poly_degree(eq, vars))
        f += p * eq
    for i, ineq in enumerate(ineqs):
        s = poly_variable(f'd{i}', vars, (2*deg - poly_degree(ineq, vars))//2*2)
        prob.add_sos_constraint(s, vars, name=f'd{i}', sparse=sparse)
        f += s * ineq

    i = 0
    if ineq_prods:
        for r in range(len(ineqs)):
            for comb in combinations(ineqs, r):
                total_deg = sum(map(lambda p: sp.poly(p, vars).total_degree(), comb))
                if total_deg <= 2*deg:
                    s = poly_variable(f'e{i}', vars, (2*deg - total_deg)//2*2)
                    i += 1
                    prob.add_sos_constraint(s, vars, name=f'e{i}', sparse=sparse)
                    f += s * prod(comb)

    # Much faster after using sp.expand
    prob.add_sos_constraint(sp.expand(obj - gamma - f), vars, sparse=sparse)
    prob.set_objective('max', gamma_p)
    return prob

def poly_cert_prob(vars       : List[sp.Symbol],
                   poly       : sp.Expr,
                   eqs        : Optional[List[sp.expr]] = None,
                   ineqs      : Optional[List[sp.expr]] = None,
                   ineq_prods : bool = False,
                   deg        : Optional[int] = None,
                   sparse     : bool = False) -> SOSProblem:
    '''Formulates and returns a degree DEG Sum-of-Squares relaxation of a polynomial
    optimization problem in variables VARS that certifies POLY is a sum of
    squares on the set defined by EQS and INEQS. INEQ_PRODS determines if
    products of inequalities are used. SPARSE uses Newton polytope reduction to
    do computations in a reduced-size basis. Returns an instance of SOSProblem.

    '''
    prob = SOSProblem()
    eqs, ineqs = eqs or [], ineqs or []
    deg = get_poly_degree(vars, [poly] + eqs + ineqs, deg=deg)

    f = 0 # obviously non-negative polynomial for (in)equalities constraints
    for i, eq in enumerate(eqs):
        p = poly_variable(f'c{i}', vars, 2*deg - poly_degree(eq, vars))
        f += p * eq

    if ineq_prods:
        i = 0
        for r in range(len(ineqs)):
            for comb in combinations(ineqs, r):
                total_deg = sum(map(lambda p: sp.poly(p, vars).total_degree(), comb))
                if total_deg <= 2*deg:
                    s = poly_variable(f'e{i}', vars, (2*deg - total_deg)//2*2)
                    prob.add_sos_constraint(s, vars, name=f'e{i}', sparse=sparse)
                    f += s * prod(comb)
                    i += 1
    else:
        for i, ineq in enumerate(ineqs):
            s = poly_variable(f'd{i}', vars, (2*deg - poly_degree(ineq, vars))//2*2)
            prob.add_sos_constraint(s, vars, name=f'd{i}', sparse=sparse)
            f += s * ineq

    prob.add_sos_constraint(poly - f, vars, sparse=sparse)
    return prob

class SOSConstraint:
    '''Defines a Sum-of-Squares constraint, returned by
    SOSProblem.add_sos_constraint. Holds information about the SoS constraint
    and its dual, and allows one to compute the pseudoexpectation of any
    polynomial.
    '''
    def __init__(self,
                 pic_const: pic.constraints.Constraint,
                 Q        : pic.expressions.variables.SymmetricVariable,
                 basis    : Basis,
                 symbols  : List[sp.Symbol],
                 deg      : int):
        self.pic_const = pic_const
        self.Q = Q
        self.basis = basis
        self.symbols = symbols
        self.b_sym = basis.to_sym(symbols)
        self.deg = deg

    @property
    def Qval(self):
        '''Optimization variable Q where p(x) = b^T Q b, where p(x) is polynomial
        constrained to be SoS, and b is the basis.'''
        if self.Q.value is None:
            raise ValueError('Missing value for sos constraint variable!'
                             ' (is the problem solved?)')
        return self.Q.value

    def get_chol_factor(self) -> np.array:
        '''Returns L, the Cholesky factorization of Q = LL^T. Adds a small
        multiple of identity to Q if it has small negative eigenvalues.
        '''
        mineig = min(min(np.linalg.eigh(self.Qval)[0]), 0)
        return np.linalg.cholesky(self.Qval - np.eye(len(self.basis))*mineig*1.1)

    def get_sos_decomp(self, precision: int=3) -> sp.Matrix:
        '''Returns a vector containing the sum of squares decompositon of this
        constraint'''
        L = sp.Matrix(self.get_chol_factor())
        S = (L.T @ sp.Matrix(self.b_sym)).applyfunc(lambda x: x**2)
        return round_sympy_expr(S, precision)

    def pexpect(self, expr: sp.Expr) -> sp.Expr:
        '''Computes the pseudoexpectation of a given polynomial EXPR'''
        poly = sp.poly(expr, self.symbols)
        self.basis.check_can_represent(poly)
        Qp = self.basis.sos_sym_poly_repr(poly)
        X = sp.Matrix(len(self.basis), len(self.basis), self.pic_const.dual)
        return sum(sp.matrix_multiply_elementwise(X, Qp))
