""" This module contains some auxiliary functions for the PYTHON
implementations of the core functions.
"""

# standard library
import numpy as np


def simulate_emax(num_periods, num_draws, period, k, eps_relevant,
        payoffs_ex_ante, edu_max, edu_start, emax, states_all,
        mapping_state_idx, delta):
    """ Simulate expected future value.
    """
    # Initialize containers
    emax_simulated, payoffs_ex_post, future_payoffs = 0.0, 0.0, 0.0

    # Calculate maximum value
    for i in range(num_draws):

        # Select disturbances for this draw
        disturbances = eps_relevant[i, :]

        # Get total value of admissible states
        total_payoffs, payoffs_ex_post, future_payoffs = get_total_value(period,
            num_periods, delta, payoffs_ex_ante, disturbances, edu_max,
            edu_start, mapping_state_idx, emax, k, states_all)

        # Determine optimal choice
        maximum = max(total_payoffs)

        # Recording expected future value
        emax_simulated += maximum

    # Scaling
    emax_simulated = emax_simulated / num_draws

    # Finishing
    return emax_simulated, payoffs_ex_post, future_payoffs


def get_total_value(period, num_periods, delta, payoffs_ex_ante,
                    disturbances, edu_max, edu_start, mapping_state_idx,
                    emax, k, states_all):
    """ Get total value of all possible states.
    """
    # Auxiliary objects
    is_myopic = (delta == 0.00)

    # Initialize containers
    payoffs_ex_post = np.tile(np.nan, 4)

    # Calculate ex post payoffs
    for j in [0, 1]:
        payoffs_ex_post[j] = payoffs_ex_ante[j] * disturbances[j]

    for j in [2, 3]:
        payoffs_ex_post[j] = payoffs_ex_ante[j] + disturbances[j]

    # Get future values
    if period != (num_periods - 1):
        future_payoffs = _get_future_payoffs(edu_max, edu_start,
                            mapping_state_idx, period, emax, k, states_all)
    else:
        future_payoffs = np.tile(0.0, 4)

    # Calculate total utilities
    total_payoffs = payoffs_ex_post + delta * future_payoffs

    # Special treatment in case of myopic agents
    if is_myopic:
        total_payoffs = _stabilize_myopic(total_payoffs, future_payoffs)

    # Finishing
    return total_payoffs, payoffs_ex_post, future_payoffs


''' Private functions
'''


def _get_future_payoffs(edu_max, edu_start, mapping_state_idx, period, emax, k,
        states_all):
    """ Get future payoffs for additional choices.
    """

    # Distribute state space
    exp_A, exp_B, edu, edu_lagged = states_all[period, k, :]

    # Future utilities
    future_payoffs = np.tile(np.nan, 4)

    # Working in occupation A
    future_idx = mapping_state_idx[period + 1, exp_A + 1, exp_B, edu, 0]
    future_payoffs[0] = emax[period + 1, future_idx]

    # Working in occupation B
    future_idx = mapping_state_idx[period + 1, exp_A, exp_B + 1, edu, 0]
    future_payoffs[1] = emax[period + 1, future_idx]

    # Increasing schooling. Note that adding an additional year
    # of schooling is only possible for those that have strictly
    # less than the maximum level of additional education allowed.
    if edu < edu_max - edu_start:
        future_idx = mapping_state_idx[period + 1, exp_A, exp_B, edu + 1, 1]
        future_payoffs[2] = emax[period + 1, future_idx]
    else:
        future_payoffs[2] = -np.inf

    # Staying at home
    future_idx = mapping_state_idx[period + 1, exp_A, exp_B, edu, 0]
    future_payoffs[3] = emax[period + 1, future_idx]

    # Finishing
    return future_payoffs


def _stabilize_myopic(total_payoffs, future_payoffs):
    """ Ensuring that schooling does not increase beyond the maximum allowed
    level. This is necessary as in the special case where delta is equal to
    zero, (-np.inf * 0.00) evaluates to NAN. This is returned as the maximum
    value when calling np.argmax.
    """
    # Determine NAN
    is_inf = np.isneginf(future_payoffs)

    # Replace with negative infinity
    total_payoffs[is_inf] = -np.inf

    # Finishing
    return total_payoffs