"""This module contains the code for approximate solutions to the DCDP."""
import warnings

import numba as nb
import numpy as np

from respy.config import MAX_LOG_FLOAT
from respy.parallelization import combine_and_split_interpolation
from respy.parallelization import parallelize_across_dense_dimensions
from respy.shared import calculate_expected_value_functions
from respy.shared import calculate_value_functions_and_flow_utilities


def interpolate(state_space, period_draws_emax_risk, period, optim_paras, options):
    """Interface to switch between different interpolation routines."""
    period_expected_value_functions = _kw_94_interpolation(
        state_space, period_draws_emax_risk, period, optim_paras, options
    )

    return period_expected_value_functions


def _kw_94_interpolation(
    state_space, period_draws_emax_risk, period, optim_paras, options
):
    r"""Calculate the approximate solution proposed by [1]_.

    The authors propose an interpolation method to alleviate the computation burden of
    the full solution. The full solution calculates the expected value function with
    Monte-Carlo simulation for each state in the state space for a pre-defined number of
    points. Both, the number of states and points, have a huge impact on runtime.

    [1]_ propose an interpolation method to alleviate the computation burden. The
    general idea is to calculate the expected value function with Monte-Carlo simulation
    only for a much smaller number of states and predict the remaining expected value
    functions with a linear model. The linear model is

    .. math::

        EVF - MaxeVF = \pi_0 + \sum^{n-1}_{i=0} \pi_{1i} (MaxeVF - eVF_i)
                             + \sum^{n-1}_{j=0} \pi_{2j} \sqrt{MaxeVF - eVF_j}

    where :math:`EVF` are the expected value functions generated by the Monte-Carlo
    simulation, :math:`eVF_i` are the value functions generated with the expected value
    of the shocks, and :math:`MaxeVF` is their maximum over all :math:`i`.

    The expected value of the shocks is zero for non-working alternatives. For working
    alternatives, the shocks are log normally distributed and cannot be set to zero, but
    :math:`E(X) = \exp\{\mu + \frac{\sigma^2}{2}\}` where :math:`\mu = 0`.

    After experimenting with various functions for :math:`g()`, the authors include
    simple differences and the square root of the simple differences in the equation.

    The function consists of the following steps.

    1. Create an indicator for whether the expected value function of the state is
       calculated with Monte-Carlo simulation or interpolation.

    2. Compute the expected value of the shocks.

    3. Compute the right-hand side variables of the linear model.

    4. Compute the left-hand side variables of the linear model by Monte-Carlo
       simulation on subset of states.

    5. Fit the linear model with ordinary least squares on the subset without
       interpolation and predict the expected value functions for all other states.

    References
    ----------
    .. [1] Keane, M. P. and  Wolpin, K. I. (1994). `The Solution and Estimation of
           Discrete Choice Dynamic Programming Models by Simulation and Interpolation:
           Monte Carlo Evidence <https://doi.org/10.2307/2109768>`_. *The Review of
           Economics and Statistics*, 76(4): 648-672.

    """
    n_wages = len(optim_paras["choices_w_wage"])
    n_core_states_in_period = state_space.core.query("period == @period").shape[0]

    seed = _get_seeds_for_interpolation(state_space, options)
    interp_points = _split_interpolation_points_evenly(state_space, options)

    not_interpolated = _get_not_interpolated_indicator(
        interp_points, n_core_states_in_period, seed
    )

    # Create an array with the expected value of the shocks.
    expected_shocks = np.zeros(len(optim_paras["choices"]))
    var = np.diag(optim_paras["shocks_cholesky"].dot(optim_paras["shocks_cholesky"].T))
    expected_shocks[:n_wages] = np.exp(np.clip(var[:n_wages], 0, MAX_LOG_FLOAT) / 2)

    wages = state_space.get_attribute_from_period("wages", period)
    nonpecs = state_space.get_attribute_from_period("nonpecs", period)
    continuation_values = state_space.get_continuation_values(period=period)

    exogenous, max_emax = _compute_rhs_variables(
        wages, nonpecs, continuation_values, expected_shocks, optim_paras["delta"]
    )

    endogenous = _compute_lhs_variable(
        wages,
        nonpecs,
        continuation_values,
        max_emax,
        not_interpolated,
        period_draws_emax_risk,
        optim_paras["delta"],
    )

    # Create prediction model based on the random subset of points where the EMAX is
    # actually simulated and thus dependent and independent variables are available. For
    # the interpolation points, the actual values are used.
    period_expected_value_functions = _predict_with_linear_model(
        endogenous, exogenous, max_emax, not_interpolated
    )

    return period_expected_value_functions


def _get_seeds_for_interpolation(state_space, options):
    if hasattr(state_space, "sub_state_spaces"):
        seed = {
            dense_idx: next(options["solution_seed_iteration"])
            for dense_idx in state_space.sub_state_spaces
        }
    else:
        seed = next(options["solution_seed_iteration"])

    return seed


def _split_interpolation_points_evenly(state_space, options):
    """Split the number of interpolated states evenly across dense dimensions."""
    if hasattr(state_space, "sub_state_spaces"):
        n_dense_combinations = len(state_space.sub_state_spaces)
        interp_points = options["interpolation_points"] // np.full(
            n_dense_combinations, n_dense_combinations
        )
        remaining_points = options["interpolation_points"] % n_dense_combinations
        interp_points[:remaining_points] += 1
        interp_points = {
            dense_idx: points
            for dense_idx, points in zip(state_space.sub_state_spaces, interp_points)
        }
    else:
        interp_points = options["interpolation_points"]

    return interp_points


@parallelize_across_dense_dimensions
def _get_not_interpolated_indicator(interpolation_points, n_states, seed):
    """Get indicator for states which will be not interpolated.

    Parameters
    ----------
    interpolation_points : int
        Number of states which will be interpolated.
    n_states : int
        Total number of states in period.
    seed : int
        Seed to set randomness.

    Returns
    -------
    not_interpolated : numpy.ndarray
        Array of shape (n_states,) indicating states which will not be interpolated.

    """
    np.random.seed(seed)

    indices = np.random.choice(n_states, size=interpolation_points, replace=False)
    not_interpolated = np.zeros(n_states, dtype="bool")
    not_interpolated[indices] = True

    return not_interpolated


@parallelize_across_dense_dimensions
def _compute_rhs_variables(wages, nonpec, emaxs, draws, delta):
    """Compute right-hand side variables of the linear model.

    Constructing the exogenous variable for all states, including the ones where
    simulation will take place. All information will be used in either the construction
    of the prediction model or the prediction step.

    Parameters
    ----------
    wages : numpy.ndarray
        Array with shape (n_states_in_period, n_choices).
    nonpec : numpy.ndarray
        Array with shape (n_states_in_period, n_choices).
    emaxs : numpy.ndarray
        Array with shape (n_states_in_period, n_choices).
    draws : numpy.ndarray
        Array with shape (n_choices,).
    delta : float
        Discount factor.

    Returns
    -------
    exogenous : numpy.ndarray
        Array with shape (n_states_in_period, n_choices * 2 + 1) where the last column
        contains the constant.
    max_value_functions : numpy.ndarray
        Array with shape (n_states_in_period,) containing maximum over all value
        functions computed with the expected value of shocks.

    """
    value_functions, _ = calculate_value_functions_and_flow_utilities(
        wages, nonpec, emaxs, draws, delta
    )

    max_value_functions = value_functions.max(axis=1)
    exogenous = max_value_functions.reshape(-1, 1) - value_functions

    exogenous = np.column_stack(
        (exogenous, np.sqrt(exogenous), np.ones(exogenous.shape[0]))
    )

    return exogenous, max_value_functions


@parallelize_across_dense_dimensions
def _compute_lhs_variable(
    wages,
    nonpec,
    continuation_values,
    max_value_functions,
    not_interpolated,
    draws,
    delta,
):
    """Calculate left-hand side variable for all states which are not interpolated.

    The function computes the full solution for a subset of states. Then, the dependent
    variable is the expected value function minus the maximum of value function with the
    expected shocks.

    Parameters
    ----------
    wages : numpy.ndarray
        Array with shape (n_states_in_period, n_choices).
    nonpec : numpy.ndarray
        Array with shape (n_states_in_period, n_choices).
    continuation_values : numpy.ndarray
        Array with shape (n_states_in_period, n_choices).
    max_value_functions : numpy.ndarray
        Array with shape (n_states_in_period,) containing maximum over all value
        functions computed with the expected value of shocks.
    not_interpolated : numpy.ndarray
        Array with shape (n_states_in_period,) containing indicators for simulated
        continuation_values.
    draws : numpy.ndarray
        Array with shape (n_draws, n_choices) containing draws.
    delta : float
        Discount factor.

    """
    expected_value_functions = calculate_expected_value_functions(
        wages[not_interpolated],
        nonpec[not_interpolated],
        continuation_values[not_interpolated],
        draws,
        delta,
    )
    endogenous = expected_value_functions - max_value_functions[not_interpolated]

    return endogenous


@combine_and_split_interpolation
def _predict_with_linear_model(
    endogenous, exogenous, max_value_functions, not_interpolated
):
    """Predict the expected value function for interpolated states with a linear model.

    The linear model is fitted with ordinary least squares. Then, predict the expected
    value function for all interpolated states and use the compute expected value
    functions for the remaining states.

    Parameters
    ----------
    endogenous : numpy.ndarray
        Array with shape (num_simulated_states_in_period,) containing the expected value
        functions minus the maximufor states used to interpolate the rest.
    exogenous : numpy.ndarray
        Array with shape (n_states_in_period, n_choices * 2 + 1) containing exogenous
        variables.
    max_value_functions : numpy.ndarray
        Array with shape (n_states_in_period,) containing maximum over all value
        functions computed with the expected value of shocks.
    not_interpolated : numpy.ndarray
        Array with shape (n_states_in_period,) containing indicator for states which
        are not interpolated and used to estimate the coefficients for the
        interpolation.

    """
    beta = ols(endogenous, exogenous[not_interpolated])

    endogenous_predicted = exogenous.dot(beta)
    endogenous_predicted = np.clip(endogenous_predicted, 0, None)

    predictions = endogenous_predicted + max_value_functions
    predictions[not_interpolated] = endogenous + max_value_functions[not_interpolated]

    if not np.all(np.isfinite(beta)):
        warnings.warn("OLS coefficients in the interpolation are not finite.")

    return predictions


@nb.njit
def ols(y, x):
    """Calculate the coefficients of a linear model with OLS using a pseudo-inverse.

    Parameters
    ----------
    x : numpy.ndarray
        Array with shape (n_observations, n_independent_variables) containing the
        independent variables.
    y : numpy.ndarray
        Array with shape (n_observations,) containing the dependent variable.

    Returns
    -------
    beta : numpy.ndarray
        Array with shape (n_independent_variables,) containing the coefficients of the
        linear model.

    """
    beta = np.dot(np.linalg.pinv(x.T.dot(x)), x.T.dot(y))
    return beta