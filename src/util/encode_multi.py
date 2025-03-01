
import numpy as np

from util.constants import MAX_SAT

MAX_SAT = 8
PAST_LEN = 8
# PAST_SAT_LOG_LEN = 1


def encode_other_sat_info(sat_decision_log, num_agents, cur_sat_id, next_sat_id, agent, other_sat_users
                          , other_sat_bw_logs, past_sat_log_len):
    # self.sat_decision_log
    # one hot encoding by bw strength
    # MAX_SAT
    assert len(other_sat_users.keys()) == len(other_sat_bw_logs.keys())
    other_user_sat_decisions = []
    other_sat_num_users = []
    other_sat_bws = []
    other_sat_id_bw = {}
    other_index_ids = {}
    cur_user_sat_decisions = []

    """
    for sat_id in other_sat_bw_logs.keys():
        avg_bw = sum(other_sat_bw_logs[sat_id]) / len(other_sat_bw_logs[sat_id])
        other_sat_id_bw[sat_id] = avg_bw
    """
    other_ids = sorted(other_sat_users, reverse=True)
    other_ids = other_ids[:MAX_SAT - 2]

    for i in range(MAX_SAT - 2):
        if len(other_ids) <= i:
            break
        other_index_ids[other_ids[i]] = i

    for i in range(MAX_SAT - 2):
        if len(other_sat_users.keys()) <= i:
            other_sat_num_users.append(0)
            other_sat_bws.append([0, 0, 0, 0, 0, 0, 0, 0])
            continue
        other_sat_num_users.append(other_sat_users[other_ids[i]])
        if len(other_sat_bw_logs[other_ids[i]]) < PAST_LEN:
            tmp_len = len(other_sat_bw_logs[other_ids[i]])
            other_sat_bws.append([0] * (PAST_LEN - tmp_len) + other_sat_bw_logs[other_ids[i]])
        else:
            other_sat_bws.append(other_sat_bw_logs[other_ids[i]])

    for index, i_agent in enumerate(range(num_agents)):
        # Exclude the current user's decision
        sat_logs = sat_decision_log[i_agent][-past_sat_log_len:]

        tmp_logs = []
        i = 0
        for log_data in sat_logs:
            if log_data == cur_sat_id:
                encoded_logs = [1] + [0] * (MAX_SAT - 1)
            elif log_data == next_sat_id:
                encoded_logs = [0] * (MAX_SAT - 1) + [1]
            elif log_data in other_index_ids.keys() and i <= 2:
                tmp_array = [0] * (i + 2) + [1] + [0] * (MAX_SAT - i - 3)
                i += 1
                # tmp_array[other_index_ids[log_data] + 2] = 1
                encoded_logs = tmp_array
            elif log_data == -1:
                encoded_logs = [0] * MAX_SAT
            else:
                # print("Warning: More than 8 visible satellites?!")
                # encoded_logs = [0, 0, 0, 0, 0, 0, 0, 0]
                encoded_logs = [0] * MAX_SAT
            # encoded_logs = encoded_logs + [0] * 3
            tmp_logs.append(encoded_logs)
        if i_agent == agent:
            cur_user_sat_decisions.append(tmp_logs)
        else:
            other_user_sat_decisions.append(tmp_logs)

    return other_ids, other_user_sat_decisions, other_sat_num_users, other_sat_bws, cur_user_sat_decisions


def one_hot_encode(input_list, size):
    input_list = np.array(input_list)
    input_list = input_list.reshape(len(input_list), 1)

    onehot_encoded = []
    for tmp_list in input_list:
        onehot_encoded.append([0] * tmp_list[0] + [1] + [0] * (size -tmp_list[0] - 1))
    return onehot_encoded