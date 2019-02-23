from __future__ import division
import time
import get_initial_network as gia
import pandas as pd
import numpy as np
from pyomo.environ import *
from lp_config import *
import generate_testcase

# ============================
# Auxiliary functions for LP
# ============================


def initial_network():
    """
    Initiate data of main problem

    :param None
    :type Nonetype

    :returns: points_on_line: information about every node in study case
    :rtype: GeoDataFrame
    :returns: tranches: information about every tranch in study case (splitted street network)
    :rtype: GeoDataFrame
    :returns: dict_length: length on street network between every node
    :rtype: dictionary
    :returns: dict_path: list of edges between two nodes
    :rtype: dictionary
    """

    gia.calc_substation_location()
    points_on_line, tranches = gia.connect_building_to_grid()
    points_on_line_processed = gia.process_network(points_on_line)
    dict_length, dict_path = gia.create_length_dict(points_on_line_processed, tranches)
    return points_on_line_processed, tranches, dict_length, dict_path


def annuity_factor(n, i):
    """
    Calculates the annuity factor derived by formula (1+i)**n * i / ((1+i)**n - 1)

    :param n: depreciation period (40 = 40 years)
    :type int
    :param i: interest rate (0.06 = 6%)
    :type float

    :returns: a: annuity factor
    :rtype: float
    """

    a = (1 + i) ** n * i / ((1 + i) ** n - 1)

    return a

# ============================
# Objective function for LP
# ============================


def cost_rule(m, cost_type):
    """
    Cost rules of objective function. Calculation is depending on type cost

    :param m: complete pyomo model
    :type pyomo model
    :param cost_type:   'investment' investment costs for electric network
                        'om' operation and maintenance cost for electric network
                        'losses' cost for power losses in lines
    :type string

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    if cost_type == 'investment':
        c_inv = 0.0
        for (i, j) in m.set_edge:
            for t in m.set_linetypes:
                c_inv += 2.0 * (m.var_x[i, j, t]
                              * m.dict_length[i, j]) \
                         * m.dict_line_tech[t]['price_sgd_per_m'] \
                         * m.dict_line_tech[t]['annuity_factor']
        return m.var_costs['investment'] == c_inv
    elif cost_type == 'om':
        # c_om = 0.0
        # for (i, j) in m.set_edge:
        #     for t in m.set_linetypes:
        #         c_om += 2 * (m.var_x[i, j, t] * m.dict_length[i, j])\
        #                 * m.dict_line_tech[t]['c_invest'] * m.dict_line_tech[t]['annuity_factor']\
        #                 * m.dict_line_tech[t]['om_factor']

        # each om factor is the same
        return m.var_costs['om'] == m.var_costs['investment'] * m.dict_line_tech[0]['om_factor']  # TODO Differentiation of types
    elif cost_type == 'losses':
        c_losses = 0.0
        for (i, j) in m.set_edge:
            for t in m.set_linetypes:
                c_losses += (m.var_power_over_line[i, j, t] * I_BASE) ** 2 \
                            * m.dict_length[i, j] * (m.dict_line_tech[t]['r_ohm_per_km'] / 1000.0) \
                            * APPROX_LOSS_HOURS \
                            * (ELECTRICITY_COST * (10**(-3)))

        return m.var_costs['losses'] == c_losses
    else:
        raise NotImplementedError("Unknown cost type!")


def obj_rule(m):
    """
    Objective function. Minimizes sum of cost rules

    :param m: complete pyomo model
    :type pyomo model

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    return sum(m.var_costs[cost_type] for cost_type in m.set_cost_type)

# ============================
# Constraint rules
# ============================


def power_balance_rule(m, i):
    """
    Power balance of network. p_node_in of node equals to p_node_out.

    :param m: complete pyomo model
    :type pyomo model
    :param i: node index of set_nodes
    :type int

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    p_node_out = float(m.dict_power_demand[i])
    p_node_in = 0.0

    if i in m.set_nodes_sub:
        p_node_in += m.var_power_sub[i]

    for j in m.set_nodes:
        if i != j and i < j:
            for t in m.set_linetypes:
                p_node_in += m.var_power_over_line[i, j, t]

    for j in m.set_nodes:
        if i != j and i > j:
            for t in m.set_linetypes:
                p_node_in -= m.var_power_over_line[j, i, t]

    return p_node_out == p_node_in


def power_over_line_rule(m, i, j, t):
    """
    Calculation of power over line with linear DC POWER FLOW MODEL.

    :param m: complete pyomo model
    :type pyomo model
    :param i: startnode index of set_edge
    :type int
    :param j: endnode index of set_edge
    :type int
    :param t: type index of set_linetypes
    :type int

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    power_over_line = ((
                        ((m.var_theta[i] - m.var_theta[j])*np.pi/180)  # rad to degree
                        / (m.dict_length[i, j] * m.dict_line_tech[t]['x_ohm_per_km'] / 1000)
                        )
                       + m.var_slack[i, j, t])
    return m.var_power_over_line[i, j, t] == power_over_line


def slack_voltage_angle_rule(m, i):
    """
    Set the voltage angle of Substation node to zero

    :param m: complete pyomo model
    :type pyomo model
    :param i: node index of set_nodes_sub
    :type int

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    return m.var_theta[i] == 0.0   # Voltage angle of slack has to be zero


def slack_pos_rule(m, i, j, t):
    """
    If decision variable m.var_x[i, j, t] is set to FALSE, the positive slack varibale var_slack[i, j, t] is nonzero

    :param m: complete pyomo model
    :type pyomo model
    :param i: startnode index of set_edge
    :type int
    :param j: endnode index of set_edge
    :type int
    :param t: type index of set_linetypes
    :type int

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    return (m.var_slack[i, j, t]) <= (1 - m.var_x[i, j, t]) * 1000000


def slack_neg_rule(m, i, j, t):
    """
    If decision variable m.var_x[i, j, t] is set to FALSE, the negative slack varibale var_slack[i, j, t] is nonzero

    :param m: complete pyomo model
    :type pyomo model
    :param i: startnode index of set_edge
    :type int
    :param j: endnode index of set_edge
    :type int
    :param t: type index of set_linetypes
    :type int

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    return (m.var_slack[i, j, t]) >= (-1) * (1 - m.var_x[i, j, t]) * 1000000


def power_over_line_rule_pos_rule(m, i, j, t):
    """
    If decision variable m.var_x[i, j, t] is set to TRUE, the positive power over the line var_power_over_line is
    limited by power_line_limit

    :param m: complete pyomo model
    :type pyomo model
    :param i: startnode index of set_edge
    :type int
    :param j: endnode index of set_edge
    :type int
    :param t: type index of set_linetypes
    :type int

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    power_line_limit = m.var_x[i, j, t]\
                       * (m.dict_line_tech[t]['I_max_A'] * V_BASE*10**3) \
                       / (S_BASE*10**6)
    return m.var_power_over_line[i, j, t] <= power_line_limit


def power_over_line_rule_neg_rule(m, i, j, t):
    """
    If decision variable m.var_x[i, j, t] is set to TRUE, the negative power over the line var_power_over_line is
    limited by power_line_limit

    :param m: complete pyomo model
    :type pyomo model
    :param i: startnode index of set_edge
    :type int
    :param j: endnode index of set_edge
    :type int
    :param t: type index of set_linetypes
    :type int

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    power_line_limit = (-1) * m.var_x[i, j, t]\
                       * (m.dict_line_tech[t]['I_max_A'] * V_BASE*10**3) \
                       / (S_BASE*10**6)
    return m.var_power_over_line[i, j, t] >= power_line_limit


def radial_rule(m):
    """
    To generate a cost efficient radial grid structure, the summation of the number of lines in the network is
    limited to the "number of nodes" - "number of substations"

    :param m: complete pyomo model
    :type pyomo model

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    return summation(m.var_x) == (len(m.set_nodes) - len(m.set_nodes_sub))


def one_linetype_rule(m, i, j):
    """
    To decrease computational time, the number of line types is limited to a maximum one between two nodes

    :param m: complete pyomo model
    :type pyomo model
    :param i: startnode index of set_edge
    :type int
    :param j: endnode index of set_edge
    :type int

    :returns: pyomo equality function
    :rtype: pyomo rule
    """

    number_of_linetypes = 0
    for t in m.set_linetypes:
        number_of_linetypes += m.var_x[i, j, t]
    return number_of_linetypes <= 1


def main():
    """
    Main function of electric grid optimization tool. Generates pyomo model of grid problem

    :param dict_connected: key is node index. Value is binary and indicates if building is connected to thermal network
    :type dictionary

    :returns: m: complete pyomo model
    :rtype: pyomo model
    """

    # ===========================================
    # Initialize Data
    # ===========================================

    # Street network and Buildings
    if SCENARIO == '/newcase/med/':
        df_nodes, tranches, dict_length, dict_path = generate_testcase.main()
    else:
        df_nodes, tranches, dict_length, dict_path = initial_network()

    # Line Parameters
    df_line_parameter = pd.read_csv(LOCATOR + '/electric_line_data.csv')
    dict_line_tech = dict(df_line_parameter.T)  # dict transposed dataframe

    # annuity factor (years, interest)
    for idx_line in dict_line_tech:
        dict_line_tech[idx_line]['annuity_factor'] = annuity_factor(40, INTEREST_RATE)

    # Cost types
    cost_types = [
        'investment',  # investment costs
        'om',  # operation and maintenance cost
        # 'losses',  # cost for power losses in lines
        ]

    # ============================
    # Index data
    # ============================

    idx_nodes_sub = df_nodes[df_nodes['Type'] == 'PLANT'].index
    idx_nodes_consum = df_nodes[df_nodes['Type'] == 'CONSUMER'].index
    idx_nodes = idx_nodes_sub.append(idx_nodes_consum)

    # Diagonal edge index matrix
    idx_edge = []
    for i in idx_nodes:
        for j in idx_nodes:
            if i != j and i < j:
                idx_edge.append((i, j))

    idx_linetypes = range(len(dict_line_tech))

    # ============================
    # Preprocess data
    # ============================

    dict_power_demand = {}
    for idx_node, node in df_nodes.iterrows():
        if not np.isnan(node['GRID0_kW']):
            dict_power_demand[idx_node] = 3.0 * (node['GRID0_kW']*10**3) \
                                          / (S_BASE*10**6)  # kW / MW

    dict_length_processed = {}
    for idx_node1, node1 in dict_length.iteritems():
        for idx_node2, length in node1.iteritems():
            dict_length_processed[idx_node1, idx_node2] = length

    # ============================
    # Model Problem
    # ============================

    m = ConcreteModel()
    m.name = 'CONCEPT - Electrical Network Opt'

    # Create Sets
    m.set_nodes = Set(initialize=idx_nodes)
    m.set_nodes_sub = Set(initialize=idx_nodes_sub)
    m.set_nodes_consum = Set(initialize=idx_nodes_consum)
    m.set_edge = Set(initialize=idx_edge)
    m.set_linetypes = Set(initialize=idx_linetypes)
    m.set_cost_type = Set(initialize=cost_types)

    # Create Parameters
    m.dict_length = dict_length_processed.copy()
    m.dict_line_tech = dict_line_tech.copy()
    m.dict_power_demand = dict_power_demand.copy()

    # Create variables
    m.var_x = Var(m.set_edge, m.set_linetypes, within=Binary)  # decision variable for lines
    m.var_theta = Var(m.set_nodes, within=Reals, bounds=(-30.0, 30.0))  # in degree
    m.var_power_sub = Var(m.set_nodes_sub, within=NonNegativeReals)
    m.var_power_over_line = Var(m.set_edge, m.set_linetypes, within=Reals)
    m.var_slack = Var(m.set_edge, m.set_linetypes, within=Reals)
    m.var_costs = Var(m.set_cost_type, within=Reals)

    # Declare constraint functions
    m.power_balance_rule_constraint = Constraint(m.set_nodes, rule=power_balance_rule)
    m.power_over_line_constraint = Constraint(m.set_edge, m.set_linetypes, rule=power_over_line_rule)
    m.slack_voltage_angle_constraint = Constraint(m.set_nodes_sub, rule=slack_voltage_angle_rule)

    m.slack_neg_constraint = Constraint(m.set_edge, m.set_linetypes, rule=slack_neg_rule)
    m.slack_pos_constraint = Constraint(m.set_edge, m.set_linetypes, rule=slack_pos_rule)
    m.power_over_line_rule_neg_constraint = Constraint(m.set_edge, m.set_linetypes, rule=power_over_line_rule_neg_rule)
    m.power_over_line_rule_pos_constraint = Constraint(m.set_edge, m.set_linetypes, rule=power_over_line_rule_pos_rule)

    m.radial_constraint = Constraint(rule=radial_rule)
    m.one_linetype = Constraint(m.set_edge, rule=one_linetype_rule)

    # Declare objective function
    m.def_costs = Constraint(
        m.set_cost_type,
        doc='Cost definitions by type',
        rule=cost_rule)
    m.obj = Objective(
        sense=minimize,
        doc='Minimize costs = investment + om + power_losses',
        rule=obj_rule)

    return m


if __name__ == '__main__':
    t0 = time.clock()
    main()
    print 'pyomo_test succeeded'
    print('total time: ', time.clock() - t0)
