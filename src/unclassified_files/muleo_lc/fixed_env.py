import numpy as np
import itertools
from statsmodels.tsa.api import ExponentialSmoothing, SimpleExpSmoothing, Holt
import pandas as pd

import numpy as np

VIDEO_BIT_RATE = [300, 750, 1200, 1850, 2850, 4300]
M_IN_K = 1000.0

MILLISECONDS_IN_SECOND = 1000.0
B_IN_MB = 1000000.0
BITS_IN_BYTE = 8.0
RANDOM_SEED = 42
VIDEO_CHUNCK_LEN = 4000.0  # millisec, every time add this amount to buffer
BITRATE_LEVELS = 6
TOTAL_VIDEO_CHUNCK = 48
BUFFER_THRESH = 60.0 * MILLISECONDS_IN_SECOND  # millisec, max buffer limit
DRAIN_BUFFER_SLEEP_TIME = 500.0  # millisec
PACKET_PAYLOAD_PORTION = 0.95
LINK_RTT = 80  # millisec
PACKET_SIZE = 1500  # bytes
VIDEO_SIZE_FILE = './envivio/video_size_'
DEFAULT_QUALITY = 1  # default video quality without agent

CHUNK_TIL_VIDEO_END_CAP = 48.0

MPC_FUTURE_CHUNK_COUNT = 5

# LEO SETTINGS
HANDOVER_DELAY = 0.2  # sec
HANDOVER_WEIGHT = 1
SCALE_VIDEO_SIZE_FOR_TEST = 20
SCALE_VIDEO_LEN_FOR_TEST = 2

# Multi-user setting
NUM_AGENTS = 16


QUALITY_FACTOR = 1
REBUF_PENALTY = 4.3  # pensieve: 4.3  # 1 sec rebuffering -> 3 Mbps
SMOOTH_PENALTY = 1

class Environment:
    def __init__(self, all_cooked_time, all_cooked_bw, random_seed=RANDOM_SEED, num_agents=NUM_AGENTS):
        assert len(all_cooked_time) == len(all_cooked_bw)

        np.random.seed(random_seed)
        self.num_agents = num_agents

        self.all_cooked_time = all_cooked_time
        self.all_cooked_bw = all_cooked_bw

        # pick a random trace file
        self.trace_idx = 0
        self.cooked_time = self.all_cooked_time[self.trace_idx]
        self.cooked_bw = self.all_cooked_bw[self.trace_idx]

        # self.last_quality = DEFAULT_QUALITY
        self.last_quality = [DEFAULT_QUALITY for _ in range(self.num_agents)]

        self.connection = {}
        for sat_id, sat_bw in self.cooked_bw.items():
            self.connection[sat_id] = -1

        self.mahimahi_start_ptr = 1
        # randomize the start point of the trace
        # note: trace file starts with time 0
        self.mahimahi_ptr = [1 for _ in range(self.num_agents)]
        self.last_mahimahi_time = [self.cooked_time[self.mahimahi_start_ptr - 1] for _ in range(self.num_agents)]

        # multiuser setting
        self.cur_sat_id = []
        self.prev_sat_id = [None for _ in range(self.num_agents)]
        for agent in range(self.num_agents):
            cur_sat_id = self.get_best_sat_id(agent)
            self.connection[cur_sat_id] = agent
            self.cur_sat_id.append(cur_sat_id)

        self.video_chunk_counter = [0 for _ in range(self.num_agents)]
        self.buffer_size = [0 for _ in range(self.num_agents)]
        self.video_chunk_counter_sent = [0 for _ in range(self.num_agents)]
        self.video_chunk_remain = [0 for _ in range(self.num_agents)]
        self.end_of_video = [False for _ in range(self.num_agents)]
        self.next_video_chunk_sizes = [[] for _ in range(self.num_agents)]
        self.bit_rate = None
        self.download_bw = [[] for _ in range(self.num_agents)]
        self.past_download_ests = [[] for _ in range(self.num_agents)]
        self.past_download_bw_errors = [[] for _ in range(self.num_agents)]
        self.past_bw_ests = [{} for _ in range(self.num_agents)]
        self.past_bw_errors = [{} for _ in range(self.num_agents)]

        # Centralization
        self.user_qoe_log = []

        self.video_size = {}  # in bytes
        for bitrate in range(BITRATE_LEVELS):
            self.video_size[bitrate] = []
            with open(VIDEO_SIZE_FILE + str(bitrate)) as f:
                for line in f:
                    self.video_size[bitrate].append(int(line.split()[0]))

    def get_video_chunk(self, quality, agent, model_type):
        
        assert quality >= 0
        assert quality < BITRATE_LEVELS
        
        video_chunk_size = self.video_size[quality][self.video_chunk_counter[agent]]
        
        # use the delivery opportunity in mahimahi
        delay = 0.0  # in ms
        video_chunk_counter_sent = 0  # in bytes
        
        is_handover = False
        
        self.last_quality[agent] = quality
        
        while True:  # download video chunk over mahimahi
            throughput = self.cooked_bw[self.cur_sat_id[agent]][self.mahimahi_ptr[agent]] \
                         * B_IN_MB / BITS_IN_BYTE
            if throughput == 0.0:
                # Do the forced handover
                # Connect the satellite that has the best serving time
                sat_id = self.get_best_sat_id(agent, self.mahimahi_ptr[agent])
                self.switch_sat(agent, sat_id)
                delay += HANDOVER_DELAY
                is_handover = True

                throughput = self.cooked_bw[self.cur_sat_id[agent]][self.mahimahi_ptr[agent]] * B_IN_MB / BITS_IN_BYTE

            duration = self.cooked_time[self.mahimahi_ptr[agent]] \
                       - self.last_mahimahi_time[agent]
                       
            packet_payload = throughput * duration * PACKET_PAYLOAD_PORTION
            
            if video_chunk_counter_sent + packet_payload > video_chunk_size:

                fractional_time = (video_chunk_size - video_chunk_counter_sent) / \
                                  throughput / PACKET_PAYLOAD_PORTION
                delay += fractional_time
                self.last_mahimahi_time[agent] += fractional_time
                break

            video_chunk_counter_sent += packet_payload
            delay += duration
            self.last_mahimahi_time[agent] = self.cooked_time[self.mahimahi_ptr[agent]]
            
            self.mahimahi_ptr[agent] += 1

            if self.mahimahi_ptr[agent] >= len(self.cooked_bw[self.cur_sat_id[agent]]):
                # loop back in the beginning
                # note: trace file starts with time 0
                self.mahimahi_ptr[agent] = 1
                self.last_mahimahi_time[agent] = 0
                self.end_of_video[agent] = True

        delay *= MILLISECONDS_IN_SECOND
        delay += LINK_RTT

        # rebuffer time
        rebuf = np.maximum(delay - self.buffer_size[agent], 0.0)

        # update the buffer
        self.buffer_size[agent] = np.maximum(self.buffer_size[agent] - delay, 0.0)

        # add in the new chunk
        self.buffer_size[agent] += VIDEO_CHUNCK_LEN

        # sleep if buffer gets too large
        sleep_time = 0
        if self.buffer_size[agent] > BUFFER_THRESH:
            # exceed the buffer limit
            # we need to skip some network bandwidth here
            # but do not add up the delay
            drain_buffer_time = self.buffer_size[agent] - BUFFER_THRESH
            sleep_time = np.ceil(drain_buffer_time / DRAIN_BUFFER_SLEEP_TIME) * \
                         DRAIN_BUFFER_SLEEP_TIME
            self.buffer_size[agent] -= sleep_time

            while True:
                duration = self.cooked_time[self.mahimahi_ptr[agent]] \
                           - self.last_mahimahi_time[agent]
                if duration > sleep_time / MILLISECONDS_IN_SECOND:
                    self.last_mahimahi_time[agent] += sleep_time / MILLISECONDS_IN_SECOND
                    break
                sleep_time -= duration * MILLISECONDS_IN_SECOND
                self.last_mahimahi_time[agent] = self.cooked_time[self.mahimahi_ptr[agent]]
                self.mahimahi_ptr[agent] += 1
                
                if self.mahimahi_ptr[agent] >= len(self.cooked_bw[self.cur_sat_id[agent]]):
                    # loop back in the beginning
                    # note: trace file starts with time 0
                    self.mahimahi_ptr[agent] = 1
                    self.last_mahimahi_time[agent] = 0
 
        # the "last buffer size" return to the controller
        # Note: in old version of dash the lowest buffer is 0.
        # In the new version the buffer always have at least
        # one chunk of video
        return_buffer_size = self.buffer_size[agent]

        self.video_chunk_counter[agent] += 1
        video_chunk_remain = TOTAL_VIDEO_CHUNCK - self.video_chunk_counter[agent]
        
        if self.video_chunk_counter[agent] >= TOTAL_VIDEO_CHUNCK:

            self.end_of_video[agent] = True
            self.buffer_size[agent] = 0
            self.video_chunk_counter[agent] = 0
            
            # Refresh satellite info
            self.connection[self.cur_sat_id[agent]] = -1
            self.cur_sat_id[agent] = -1
            
            # wait for overall clean

        next_video_chunk_sizes = []
        for i in range(BITRATE_LEVELS):
            next_video_chunk_sizes.append(self.video_size[i][self.video_chunk_counter[agent]])
            
        self.video_chunk_remain[agent] = video_chunk_remain
        self.download_bw[agent].append(float(
            video_chunk_size) / delay / M_IN_K * BITS_IN_BYTE)

        if not self.end_of_video[agent]:
            is_handover, new_sat_id, bit_rate = self.run_mpc(agent, model_type)
            if is_handover:
                delay += HANDOVER_DELAY * MILLISECONDS_IN_SECOND
                self.connection[self.cur_sat_id[agent]] = -1
                self.connection[new_sat_id] = agent
                self.prev_sat_id[agent] = self.cur_sat_id[agent]
                self.cur_sat_id[agent] = new_sat_id
                # self.download_bw[agent] = []
                # self.past_download_bw_errors[agent] = []
                # self.past_download_ests[agent] = []
        else:
            is_handover, new_sat_id, bit_rate = False, -1, 1
        
        return delay, \
            sleep_time, \
            return_buffer_size / MILLISECONDS_IN_SECOND, \
            rebuf / MILLISECONDS_IN_SECOND, \
            video_chunk_size, \
            next_video_chunk_sizes, \
            self.end_of_video[agent], \
            video_chunk_remain, \
            bit_rate, is_handover, new_sat_id
            
    def reset(self):
        
        self.video_chunk_counter = [0 for _ in range(self.num_agents)]
        self.buffer_size = [0 for _ in range(self.num_agents)]
        self.video_chunk_counter_sent = [0 for _ in range(self.num_agents)]
        self.video_chunk_remain = [0 for _ in range(self.num_agents)]
        self.end_of_video = [False for _ in range(self.num_agents)]
        self.next_video_chunk_sizes = [[] for _ in range(self.num_agents)]
        self.bit_rate = None
        self.download_bw = [[] for _ in range(self.num_agents)]
        self.past_download_ests = [[] for _ in range(self.num_agents)]
        self.past_download_bw_errors = [[] for _ in range(self.num_agents)]
        self.past_bw_ests = [{} for _ in range(self.num_agents)]
        self.past_bw_errors = [{} for _ in range(self.num_agents)]

        self.trace_idx += 1
        if self.trace_idx >= len(self.all_cooked_time):
            self.trace_idx = 0     
            
        self.cooked_time = self.all_cooked_time[self.trace_idx]
        self.cooked_bw = self.all_cooked_bw[self.trace_idx]


        self.mahimahi_ptr = [1 for _ in range(self.num_agents)]
        self.last_mahimahi_time = [self.cooked_time[self.mahimahi_start_ptr - 1] for _ in range(self.num_agents)]
        
        self.connection = {}
        for sat_id, sat_bw in self.cooked_bw.items():
            self.connection[sat_id] = -1

        self.cur_sat_id = []
        self.prev_sat_id = [None for _ in range(self.num_agents)]
        for agent in range(self.num_agents):
            cur_sat_id = self.get_best_sat_id(agent)
            self.connection[cur_sat_id] = agent
            self.cur_sat_id.append(cur_sat_id)
        
    def check_end(self):
        for agent in range(self.num_agents):
            if not self.end_of_video[agent]:
                return False
        return True

    def get_first_agent(self):
        user = -1
        
        for agent in range(self.num_agents):
            if not self.end_of_video[agent]:
                if user == -1:
                    user = agent
                else:
                    if self.last_mahimahi_time[agent] < self.last_mahimahi_time[user]:
                        user = agent

        if user == 0:
            self.user_qoe_log = []
        return user
 
    def get_best_sat_id(self, agent, mahimahi_ptr=None):
        best_sat_id = None
        best_sat_bw = 0

        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr[agent]

        for sat_id, sat_bw in self.cooked_bw.items():
            if self.connection[sat_id] == -1:
                if best_sat_bw < sat_bw[mahimahi_ptr]:
                    best_sat_id = sat_id
                    best_sat_bw = sat_bw[mahimahi_ptr]
        
        if best_sat_id == None:
            best_sat_id = self.cur_sat_id[agent]
        
        return best_sat_id

    def switch_sat(self, agent, cur_sat_id):
        pre_sat_id = self.cur_sat_id[agent]
        self.prev_sat_id[agent] = pre_sat_id
    
        self.connection[pre_sat_id] = -1
        self.connection[cur_sat_id] = agent

        self.cur_sat_id[agent] = cur_sat_id

    def run_mpc(self, agent, model_type):
        if model_type == "ManifoldMPC":
            is_handover, new_sat_id, bit_rate = self.qoe_v2(
                agent, only_runner_up=False)
        elif model_type == "DualMPC":
            is_handover, new_sat_id, bit_rate = self.qoe_v2(
                agent, only_runner_up=True)
        elif model_type == "DualMPC-Centralization":
            is_handover, new_sat_id, bit_rate = self.qoe_v2(
                agent, centralized=True)
        else:
            print("Cannot happen!")
            exit(-1)
        return is_handover, new_sat_id, bit_rate

    def qoe_v2(self, agent, only_runner_up=True, centralized=False):
        is_handover = False
        best_sat_id = self.cur_sat_id[agent]

        ho_sat_id, ho_stamp, best_combo, max_reward = self.calculate_mpc_with_handover(
            agent, only_runner_up=only_runner_up, centralized=centralized)
        if ho_stamp == 0:
            is_handover = True
            best_sat_id = ho_sat_id
        
        bit_rate = best_combo[0]

        return is_handover, best_sat_id, bit_rate

    def calculate_mpc_with_handover(self, agent, robustness=True, only_runner_up=True,
                                    method="harmonic-mean", centralized=True):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = self.video_chunk_remain[agent]
        # last_index = self.get_total_video_chunk() - video_chunk_remain
        last_index = int(CHUNK_TIL_VIDEO_END_CAP - video_chunk_remain)

        chunk_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(BITRATE_LEVELS)), repeat=MPC_FUTURE_CHUNK_COUNT):
            chunk_combo_option.append(combo)

        future_chunk_length = MPC_FUTURE_CHUNK_COUNT
        if video_chunk_remain < MPC_FUTURE_CHUNK_COUNT:
            future_chunk_length = video_chunk_remain
        
        max_reward = -10000000
        best_combo = (self.last_quality[agent], )
        ho_sat_id = self.cur_sat_id[agent]
        ho_stamp = MPC_FUTURE_CHUNK_COUNT
        if future_chunk_length == 0:
            return ho_sat_id, ho_stamp, best_combo, max_reward

        cur_download_bw, runner_up_sat_id = None, None
        if method == "harmonic-mean":
            cur_download_bw = self.predict_download_bw(agent, True)
            runner_up_sat_id, _ = self.get_runner_up_sat_id(
                agent, method="harmonic-mean")
        elif method == "holt-winter":
            cur_download_bw = self.predict_download_bw_holt_winter(agent)
            # cur_download_bw = self.predict_download_bw(agent, True)
            runner_up_sat_id, _ = self.get_runner_up_sat_id(
                agent, method="holt-winter")
        else:
            print("Cannot happen")
            raise Exception

        start_buffer = self.buffer_size[agent] / MILLISECONDS_IN_SECOND

        best_combo, max_reward, best_case = self.calculate_mpc(video_chunk_remain, start_buffer, last_index, cur_download_bw, agent, centralized)

        for next_sat_id, next_sat_bw in self.cooked_bw.items():

            if next_sat_id == self.cur_sat_id[agent]:
                continue
            else:
                # Check if it is visible now
                if self.cooked_bw[next_sat_id][self.mahimahi_ptr[agent] - 1] != 0.0 and self.cooked_bw[next_sat_id][self.mahimahi_ptr[agent]] != 0.0:
                    # Pass the previously connected satellite
                    if next_sat_id == self.prev_sat_id[agent]:
                        continue

                    if only_runner_up and runner_up_sat_id != next_sat_id:
                        # Only consider the next-best satellite
                        continue
                    # Based on the bw, not download bw
                    next_download_bw = None
                    if method == "harmonic-mean":
                        next_download_bw = cur_download_bw * self.predict_bw(next_sat_id, agent, robustness) / self.cooked_bw[self.cur_sat_id[agent]][self.mahimahi_ptr[agent]-1]
                    elif method == "holt-winter":
                        # next_harmonic_bw = self.predict_bw_holt_winter(next_sat_id, mahimahi_ptr, num=1)
                        # Change to proper download bw
                        next_download_bw = cur_download_bw * self.cooked_bw[next_sat_id][self.mahimahi_ptr[agent]-1] /\
                            self.cooked_bw[self.cur_sat_id[agent]][self.mahimahi_ptr[agent]-1]
                    else:
                        print("Cannot happen")
                        raise Exception

                    for ho_index in range(MPC_FUTURE_CHUNK_COUNT):
                        # all possible combinations of 5 chunk bitrates for 6 bitrate options (6^5 options)
                        # iterate over list and for each, compute reward and store max reward combination
                        # ho_index: 0-4 -> Do handover, 5 -> Do not handover
                        for full_combo in chunk_combo_option:
                            # Break at the end of the chunk
                            combo = full_combo[0: future_chunk_length]
                            # calculate total rebuffer time for this combination (start with start_buffer and subtract
                            # each download time and add 2 seconds in that order)
                            curr_rebuffer_time = 0
                            curr_buffer = start_buffer
                            bitrate_sum = 0
                            smoothness_diffs = 0
                            last_quality = self.last_quality[agent]

                            for position in range(0, len(combo)):
                                chunk_quality = combo[position]
                                index = last_index + position# e.g., if last chunk is 3, then first iter is 3+0+1=4
                                download_time = 0
                                if ho_index > position:
                                    harmonic_bw = cur_download_bw
                                elif ho_index == position:
                                    harmonic_bw = next_download_bw
                                    # Give them a penalty
                                    download_time += HANDOVER_DELAY
                                else:
                                    harmonic_bw = next_download_bw
                                download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                    / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                                if curr_buffer < download_time:
                                    curr_rebuffer_time += (download_time -
                                                           curr_buffer)
                                    curr_buffer = 0.0
                                else:
                                    curr_buffer -= download_time
                                curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                                # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                                # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                                bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                                smoothness_diffs += abs(
                                    VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                                last_quality = chunk_quality
                            # compute reward for this combination (one reward per 5-chunk combo)

                            # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

                            # 10~140 - 0~100 - 0~130
                            reward = bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                                - SMOOTH_PENALTY * smoothness_diffs / M_IN_K

                            if centralized:
                                for qoe_log in self.user_qoe_log:
                                    reward += self.get_mpc_qoe(qoe_log)

                            if reward > max_reward:
                                best_combo = combo
                                max_reward = reward
                                ho_sat_id = next_sat_id
                                ho_stamp = ho_index
                                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                                             "last_index": last_index, "combo": combo, "next_download_bw": next_download_bw,
                                             "ho_index": ho_index, "next_sat_id": next_sat_id}
                            elif reward == max_reward and (combo[0] >= best_combo[0] or ho_index >= 0):
                                best_combo = combo
                                max_reward = reward
                                ho_sat_id = next_sat_id
                                ho_stamp = ho_index
                                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                                             "last_index": last_index, "combo": combo,
                                             "next_download_bw": next_download_bw,
                                             "ho_index": ho_index, "next_sat_id": next_sat_id}

        self.user_qoe_log.append(best_case)
        return ho_sat_id, ho_stamp, best_combo, max_reward

    def predict_download_bw_holt_winter(self, agent, m=172):
        cur_sat_past_list = self.download_bw[agent]
        if len(cur_sat_past_list) <= 1:
            return self.download_bw[agent][-1]
        past_bws = cur_sat_past_list[-MPC_FUTURE_CHUNK_COUNT:]
        # past_bws = cur_sat_past_list
        # print(past_bws)
        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        cur_sat_past_bws = pd.Series(past_bws)
        cur_sat_past_bws.index.freq = 's'

        # alpha = 1 / (2 * m)
        fitted_model = ExponentialSmoothing(
            cur_sat_past_bws, trend='add').fit()
        # fitted_model = ExponentialSmoothing(cur_sat_past_bws, trend='mul').fit()

        # fitted_model = ExponentialSmoothing(cur_sat_past_bws
        # test_predictions = fitted_model.forecast(3)
        test_predictions = fitted_model.forecast(1)

        pred_bw = sum(test_predictions) / len(test_predictions)

        return pred_bw
        

    def get_runner_up_sat_id(self, agent, method="holt-winter"):
        best_sat_id = None
        best_sat_bw = 0

        for sat_id, sat_bw in self.cooked_bw.items():
            target_sat_bw = None

            # Pass the previously connected satellite
            if sat_id == self.cur_sat_id[agent] or sat_id == self.prev_sat_id[agent]:
                continue

            if method == "harmonic-mean":
                target_sat_bw = self.predict_bw(sat_id, agent)
            elif method == "holt-winter":
                target_sat_bw = self.predict_bw_holt_winter(sat_id, agent, num=1)
                # target_sat_bw = sum(target_sat_bw) / len(target_sat_bw)
            else:
                print("Cannot happen")
                raise Exception

            assert (target_sat_bw is not None)
            if best_sat_bw < target_sat_bw:
                best_sat_id = sat_id
                best_sat_bw = target_sat_bw

        return best_sat_id, best_sat_bw

    def predict_bw_holt_winter(self, sat_id, agent, num=1):
        start_index = self.mahimahi_ptr[agent] - MPC_FUTURE_CHUNK_COUNT
        if start_index < 0:
            cur_sat_past_list = self.cooked_bw[sat_id][0:start_index+MPC_FUTURE_CHUNK_COUNT]
        else:
            cur_sat_past_list = self.cooked_bw[sat_id][start_index:start_index+MPC_FUTURE_CHUNK_COUNT]         
        
        while len(cur_sat_past_list) != 0 and cur_sat_past_list[0] == 0.0:
            cur_sat_past_list = cur_sat_past_list[1:]


        if len(cur_sat_past_list) <= 1:
            # Just past bw
            return self.cooked_bw[sat_id][self.mahimahi_ptr[agent]-1]

        cur_sat_past_bws = pd.Series(cur_sat_past_list)
        cur_sat_past_bws.index.freq = 's'

        # alpha = 1 / (2 * m)
        fitted_model = ExponentialSmoothing(
            cur_sat_past_bws, trend='add').fit()
        # fitted_model = ExponentialSmoothing(cur_sat_past_bws, trend='mul').fit()

        # fitted_model = ExponentialSmoothing(cur_sat_past_bws
        test_predictions = fitted_model.forecast(1)
        # test_predictions = fitted_model.forecast(num)

        pred_bw = sum(test_predictions) / len(test_predictions)

        return pred_bw
        # return list(test_predictions)

    def calculate_mpc(self, video_chunk_remain, start_buffer, last_index, cur_download_bw, agent, centralized=False):
        max_reward = -10000000
        best_combo = ()
        chunk_combo_option = []
        best_case = {}
        
        # make chunk combination options
        for combo in itertools.product(list(range(BITRATE_LEVELS)), repeat=MPC_FUTURE_CHUNK_COUNT):
            chunk_combo_option.append(combo)

        future_chunk_length = MPC_FUTURE_CHUNK_COUNT
        if video_chunk_remain < MPC_FUTURE_CHUNK_COUNT:
            future_chunk_length = video_chunk_remain

        for full_combo in chunk_combo_option:
            # Break at the end of the chunk
            combo = full_combo[0: future_chunk_length]
            # calculate total rebuffer time for this combination (start with start_buffer and subtract
            # each download time and add 2 seconds in that order)
            curr_rebuffer_time = 0
            curr_buffer = start_buffer
            bitrate_sum = 0
            smoothness_diffs = 0
            last_quality = self.last_quality[agent]

            for position in range(0, len(combo)):
                chunk_quality = combo[position]
                index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                download_time = 0
                download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                 / cur_download_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                if curr_buffer < download_time:
                    curr_rebuffer_time += (download_time - curr_buffer)
                    curr_buffer = 0.0
                else:
                    curr_buffer -= download_time
                curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                smoothness_diffs += abs(
                    VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                last_quality = chunk_quality
            # compute reward for this combination (one reward per 5-chunk combo)

            # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

            # 10~140 - 0~100 - 0~130
            reward = bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                - SMOOTH_PENALTY * smoothness_diffs / M_IN_K

            """
            if ( reward >= max_reward ):
                if (best_combo != ()) and best_combo[0] < combo[0]:
                    best_combo = combo
                else:
                    best_combo = combo
                max_reward = reward
                # send data to html side (first chunk of best combo)
                send_data = 0 # no combo had reward better than -1000000 (ERROR) so send 0
                if ( best_combo != () ): # some combo was good
                    send_data = best_combo[0]
            """
            if reward > max_reward:
                best_combo = combo
                max_reward = reward
                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                             "last_index": last_index, "combo": combo, "next_download_bw": None,
                             "ho_index": MPC_FUTURE_CHUNK_COUNT, "next_sat_id": None}
            elif reward == max_reward and (combo[0] >= best_combo[0]):
                best_combo = combo
                max_reward = reward
                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                             "last_index": last_index, "combo": combo, "next_download_bw": None,
                             "ho_index": MPC_FUTURE_CHUNK_COUNT, "next_sat_id": None}

        return best_combo, max_reward, best_case

    def predict_download_bw(self, agent, robustness=False):

        curr_error = 0

        past_download_bw = self.download_bw[agent][-1]

        if len(self.past_download_ests[agent]) > 0:
            curr_error = abs(self.past_download_ests[agent][-1] - past_download_bw) / float(past_download_bw)
        self.past_download_bw_errors[agent].append(curr_error)

        # pick bitrate according to MPC
        # first get harmonic mean of last 5 bandwidths
        # past_bws = self.cooked_bw[self.cur_sat_id][start_index: self.mahimahi_ptr]
        past_bws = self.download_bw[agent][-MPC_FUTURE_CHUNK_COUNT:]
        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        bandwidth_sum = 0
        for past_val in past_bws:
            bandwidth_sum += (1 / float(past_val))

        harmonic_bw = 1.0 / (bandwidth_sum / len(past_bws))
        self.past_download_ests[agent].append(harmonic_bw)

        if robustness:
            # future bandwidth prediction
            # divide by 1 + max of last 5 (or up to 5) errors
            error_pos = -MPC_FUTURE_CHUNK_COUNT
            if len(self.past_download_bw_errors[agent]) < MPC_FUTURE_CHUNK_COUNT:
                error_pos = -len(self.past_download_bw_errors[agent])
            max_error = float(max(self.past_download_bw_errors[agent][error_pos:]))
            harmonic_bw = harmonic_bw / (1 + max_error)  # robustMPC here

        return harmonic_bw
    
    def predict_bw(self, sat_id, agent, robustness=True):
        curr_error = 0

        # past_bw = self.cooked_bw[self.cur_sat_id][self.mahimahi_ptr - 1]
        past_bw = self.cooked_bw[sat_id][self.mahimahi_ptr[agent] - 1]
        if past_bw == 0:
            return 0

        if sat_id in self.past_bw_ests[agent].keys() and len(self.past_bw_ests[agent][sat_id]) > 0 \
                and self.mahimahi_ptr[agent] - 1 in self.past_bw_ests[agent][sat_id].keys():
            curr_error = abs(self.past_bw_ests[agent][sat_id][self.mahimahi_ptr[agent] - 1] - past_bw) / float(past_bw)
        if sat_id not in self.past_bw_errors[agent].keys():
            self.past_bw_errors[agent][sat_id] = []
        self.past_bw_errors[agent][sat_id].append(curr_error)

        # pick bitrate according to MPC
        # first get harmonic mean of last 5 bandwidths
        start_index = self.mahimahi_ptr[agent] - MPC_FUTURE_CHUNK_COUNT
        if start_index < 0:
            start_index = 0

        past_bws = []
        for index in range(start_index, self.mahimahi_ptr[agent]):
            past_bws.append(self.cooked_bw[sat_id][index])

        # Newly possible satellite case
        if all(v == 0.0 for v in past_bws):
            return self.cooked_bw[sat_id][self.mahimahi_ptr[agent]]

        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        bandwidth_sum = 0
        for past_val in past_bws:
            bandwidth_sum += (1 / float(past_val))

        harmonic_bw = 1.0 / (bandwidth_sum / len(past_bws))
        if sat_id not in self.past_bw_ests[agent].keys():
            self.past_bw_ests[agent][sat_id] = {}
        if self.mahimahi_ptr[agent] not in self.past_bw_ests[agent][sat_id].keys():
            self.past_bw_ests[agent][sat_id][self.mahimahi_ptr[agent]] = None
        self.past_bw_ests[agent][sat_id][self.mahimahi_ptr[agent]] = harmonic_bw

        if robustness:
            # future bandwidth prediction
            # divide by 1 + max of last 5 (or up to 5) errors
            error_pos = -MPC_FUTURE_CHUNK_COUNT
            if sat_id in self.past_bw_errors[agent].keys() and len(
                    self.past_bw_errors[agent][sat_id]) < MPC_FUTURE_CHUNK_COUNT:
                error_pos = -len(self.past_bw_errors[agent][sat_id])
            max_error = float(max(self.past_bw_errors[agent][sat_id][error_pos:]))
            harmonic_bw = harmonic_bw / (1 + max_error)  # robustMPC here

        return harmonic_bw

    def get_mpc_qoe(self, qoe_log):
        combo = qoe_log["combo"]
        # calculate total rebuffer time for this combination (start with start_buffer and subtract
        # each download time and add 2 seconds in that order)
        curr_rebuffer_time = 0
        curr_buffer = qoe_log["start_buffer"]
        bitrate_sum = 0
        smoothness_diffs = 0
        last_quality = qoe_log["last_quality"]
        ho_index = qoe_log["ho_index"]
        for position in range(0, len(combo)):
            chunk_quality = combo[position]
            index = qoe_log["last_index"] + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
            download_time = 0
            if ho_index > position:
                harmonic_bw = qoe_log["cur_download_bw"]
            elif ho_index == position:
                harmonic_bw = qoe_log["next_download_bw"]
                # Give them a penalty
                download_time += HANDOVER_DELAY
            else:
                harmonic_bw = qoe_log["next_download_bw"]
            download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                             / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

            if curr_buffer < download_time:
                curr_rebuffer_time += (download_time -
                                       curr_buffer)
                curr_buffer = 0.0
            else:
                curr_buffer -= download_time
            curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

            # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
            # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
            bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
            smoothness_diffs += abs(
                VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
            last_quality = chunk_quality
        # compute reward for this combination (one reward per 5-chunk combo)

        # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

        # 10~140 - 0~100 - 0~130
        reward = bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                 - SMOOTH_PENALTY * smoothness_diffs / M_IN_K

        return reward
