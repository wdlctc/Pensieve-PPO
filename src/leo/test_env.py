import itertools
from typing import List, Any, Dict
import numpy as np
import pandas as pd
from statsmodels.tsa.api import ExponentialSmoothing, SimpleExpSmoothing, Holt


S_LEN = 8  # take how many frames in the past
MILLI_IN_SECOND = 1000.0
B_IN_MB = 1000000.0
DEFAULT_QUALITY = 1
BITS_IN_BYTE = 8.0
RANDOM_SEED = 42
# VIDEO_CHUNK_LEN = 4000.0  # millisec, every time add this amount to buffer
BIT_RATE_LEVELS = 6
M_IN_K = 1000.0
BUFFER_THRESH = 60.0 * MILLI_IN_SECOND  # millisec, max buffer limit
DRAIN_BUFFER_SLEEP_TIME = 500.0  # millisec
PACKET_PAYLOAD_PORTION = 0.95
LINK_RTT = 80  # millisec
NOISE_LOW = 0.9
NOISE_HIGH = 1.1
VIDEO_SIZE_FILE = 'video_server/chunk_size/video_size_'
# VIDEO_BIT_RATE = [10000, 20000, 30000, 60000, 90000, 140000]  # Kbpsz
# VIDEO_BIT_RATE = VIDEO_BIT_RATE + VIDEO_BIT_RATE
# HD_REWARD = [1, 2, 3, 6, 9, 14]
# HD_REWARD = HD_REWARD + HD_REWARD
# VIDEO_BIT_RATE = HD_REWARD

# LEO SETTINGS
HANDOVER_DELAY = 0.2  # sec
HANDOVER_WEIGHT = 1
SCALE_VIDEO_SIZE_FOR_TEST = 1
SCALE_VIDEO_LEN_FOR_TEST = 1
DEFAULT_NUMBER_OF_USERS = 30

# MPC
MPC_FUTURE_CHUNK_COUNT = 5
QUALITY_FACTOR = 1
REBUF_PENALTY = 4.3  # pensieve: 4.3  # 1 sec rebuffering -> 3 Mbps
SMOOTH_PENALTY = 1


class Environment:
    def __init__(self, all_cooked_time: list, all_cooked_bw: list, random_seed=RANDOM_SEED,
                 video_size_file=VIDEO_SIZE_FILE, video_chunk_len=None, video_bit_rate=None):
        assert video_bit_rate is not None
        assert len(all_cooked_time) == len(all_cooked_bw)

        np.random.seed(random_seed)

        self.all_cooked_time = all_cooked_time
        self.all_cooked_bw = all_cooked_bw

        self.video_chunk_counter = 0
        self.buffer_size = 0

        self.last_quality = DEFAULT_QUALITY
        # pick a random trace file
        self.trace_idx = 0
        self.cooked_time: list = self.all_cooked_time[self.trace_idx]

        # Dict: {"sat_id": [bandwidth info]}
        self.cooked_bw: dict[int:[]] = self.all_cooked_bw[self.trace_idx]

        for sat_bw in self.cooked_bw.values():
            assert len(self.cooked_time) == len(sat_bw)

        self.video_size = {}  # in bytes
        for bit_rate in range(BIT_RATE_LEVELS):
            self.video_size[bit_rate] = []
            with open(video_size_file + str(bit_rate)) as f:
                for line in f:
                    self.video_size[bit_rate].append(int(line.split()[0]) * SCALE_VIDEO_SIZE_FOR_TEST)

            # For Test
            original_list = self.video_size[bit_rate]
            for i in range(SCALE_VIDEO_LEN_FOR_TEST - 1):
                self.video_size[bit_rate].extend(original_list)

        self.video_len = len(self.video_size[0]) - 1

        self.mahimahi_start_ptr = 1
        # randomize the start point of the trace
        # note: trace file starts with time 0
        self.mahimahi_ptr = 1
        self.last_mahimahi_time = self.cooked_time[self.mahimahi_ptr - 1]

        # connect the satellite that has the best performance at first
        self.prev_sat_id = None
        self.cur_sat_id = self.get_best_sat_id()
        self.available_sat_list = self.get_available_sats_id()

        self.video_chunk_len = video_chunk_len * MILLI_IN_SECOND

        self.video_bit_rate = video_bit_rate

        self.number_of_users = DEFAULT_NUMBER_OF_USERS

        # MPC
        self.past_bw_errors: Dict[int:List[float]] = {}
        self.past_download_bw_errors = []
        self.past_bw_ests: Dict[int:List[float]] = {}
        self.past_download_ests: List[float] = []
        # self.harmonic_bw: Dict[int:float] = {}
        self.download_bw: List[float] = []
        self.mpc_result_cache: dict[int:dict[int:float]] = {}

        self.virtual_mahimahi_ptr = self.mahimahi_ptr
        self.virtual_last_mahimahi_time = self.last_mahimahi_time
        self.virtual_cur_sat_id = self.cur_sat_id

    def get_video_chunk(self, quality, handover_type="naive", test=False):
        assert quality >= 0
        assert quality < BIT_RATE_LEVELS

        video_chunk_size = self.video_size[quality][self.video_chunk_counter]  # / B_IN_MB

        # use the delivery opportunity in mahimahi
        delay = 0.0  # in ms
        video_chunk_counter_sent = 0  # in bytes

        throughput_log = []
        total_duration = 0.0

        end_of_network = False
        is_handover = False

        self.update_number_of_users()

        while True:  # download video chunk over mahimahi
            # throughput = self.cooked_bw[self.cur_sat_id][self.mahimahi_ptr] * B_IN_MB / BITS_IN_BYTE
            throughput = self.cooked_bw[self.cur_sat_id][self.mahimahi_ptr]

            if throughput == 0.0:
                # Do the forced handover
                # Connect the satellite that has the best performance at first
                print("Forced Handover")
                # print(self.cooked_bw[self.cur_sat_id][self.mahimahi_ptr-3:self.mahimahi_ptr+3])
                # print(self.mahimahi_ptr)
                self.prev_sat_id = self.cur_sat_id
                self.cur_sat_id = self.get_best_sat_id()
                self.available_sat_list = self.get_available_sats_id()
                is_handover = True
                delay += HANDOVER_DELAY
                self.download_bw = []
                self.past_download_bw_errors = []
                self.past_download_ests = []
                throughput = self.cooked_bw[self.cur_sat_id][self.mahimahi_ptr]

            assert throughput != 0.0
            throughput = throughput * B_IN_MB / BITS_IN_BYTE / self.number_of_users

            duration = self.cooked_time[self.mahimahi_ptr] \
                       - self.last_mahimahi_time
            packet_payload = throughput * duration * PACKET_PAYLOAD_PORTION

            if video_chunk_counter_sent + packet_payload > video_chunk_size:
                fractional_time = (video_chunk_size - video_chunk_counter_sent) / \
                                  throughput / PACKET_PAYLOAD_PORTION
                delay += fractional_time
                self.last_mahimahi_time += fractional_time
                throughput_log.append(throughput * fractional_time)
                total_duration += fractional_time
                break

            throughput_log.append(throughput * duration)
            total_duration += duration

            video_chunk_counter_sent += packet_payload
            delay += duration

            self.last_mahimahi_time = self.cooked_time[self.mahimahi_ptr]
            self.mahimahi_ptr += 1

            if self.mahimahi_ptr >= len(self.cooked_bw[self.cur_sat_id]):
                end_of_network = True
                break

        delay *= MILLI_IN_SECOND
        delay += LINK_RTT

        # add a multiplicative noise to the delay
        # delay *= np.random.uniform(NOISE_LOW, NOISE_HIGH)

        # rebuffer time
        rebuf = np.maximum(delay - self.buffer_size, 0.0)

        # update the buffer
        self.buffer_size = np.maximum(self.buffer_size - delay, 0.0)

        # add in the new chunk
        self.buffer_size += self.video_chunk_len

        # sleep if buffer gets too large
        sleep_time = 0
        if self.buffer_size > BUFFER_THRESH:
            # exceed the buffer limit
            # we need to skip some network bandwidth here
            # but do not add up the delay
            drain_buffer_time = self.buffer_size - BUFFER_THRESH
            sleep_time = np.ceil(drain_buffer_time / DRAIN_BUFFER_SLEEP_TIME) * \
                         DRAIN_BUFFER_SLEEP_TIME
            self.buffer_size -= sleep_time

            while True:
                if self.mahimahi_ptr >= len(self.cooked_bw[self.cur_sat_id]):
                    end_of_network = True
                    break
                duration = self.cooked_time[self.mahimahi_ptr] \
                           - self.last_mahimahi_time
                if duration > sleep_time / MILLI_IN_SECOND:
                    self.last_mahimahi_time += sleep_time / MILLI_IN_SECOND
                    break
                sleep_time -= duration * MILLI_IN_SECOND
                self.last_mahimahi_time = self.cooked_time[self.mahimahi_ptr]
                self.mahimahi_ptr += 1

                if self.mahimahi_ptr >= len(self.cooked_bw[self.cur_sat_id]):
                    end_of_network = True
                    break

        # the "last buffer size" return to the controller
        # Note: in old version of dash the lowest buffer is 0.
        # In the new version the buffer always have at least
        # one chunk of video
        return_buffer_size = self.buffer_size

        self.video_chunk_counter += 1
        video_chunk_remain = self.video_len - self.video_chunk_counter

        # Check Handover
        if not end_of_network:
            handover_result, new_sat_id = self.check_handover(handover_type)
            if handover_result:
                # print("handover")
                is_handover = handover_result
                delay += HANDOVER_DELAY * MILLI_IN_SECOND
                self.prev_sat_id = self.cur_sat_id
                self.cur_sat_id = new_sat_id
                self.available_sat_list = self.get_available_sats_id()
                self.download_bw = []
                self.past_download_bw_errors = []
                self.past_download_ests = []

        end_of_video_or_network = False
        if self.video_chunk_counter >= self.video_len or end_of_network:
            end_of_video_or_network = True
            self.buffer_size = 0
            self.video_chunk_counter = 0

            self.trace_idx += 1
            if self.trace_idx >= len(self.all_cooked_time):
                self.trace_idx = 0

            self.cooked_time: list = self.all_cooked_time[self.trace_idx]
            # Dict: {"sat_id": [bandwidth info]}
            self.cooked_bw: dict[int:[]] = self.all_cooked_bw[self.trace_idx]

            # randomize the start point of the video
            # note: trace file starts with time 0
            self.mahimahi_ptr = self.mahimahi_start_ptr
            self.last_mahimahi_time = self.cooked_time[self.mahimahi_ptr - 1]

            # Refresh satellite info
            self.prev_sat_id = self.cur_sat_id
            self.cur_sat_id = self.get_best_sat_id()
            self.available_sat_list = self.get_available_sats_id()

            self.mpc_result_cache = {}

        next_video_chunk_sizes = []
        for i in range(BIT_RATE_LEVELS):
            next_video_chunk_sizes.append(self.video_size[i][self.video_chunk_counter])

        # Record download bandwidth
        self.download_bw.append(float(video_chunk_size) / float(delay) / M_IN_K * BITS_IN_BYTE)

        self.last_quality = quality

        vis_sat_nums = len(self.get_available_sats_id())

        return delay, sleep_time, return_buffer_size / MILLI_IN_SECOND, \
               rebuf / MILLI_IN_SECOND, video_chunk_size, next_video_chunk_sizes, \
               end_of_video_or_network, video_chunk_remain, \
               self.cur_sat_id, sum(throughput_log) / total_duration, is_handover, vis_sat_nums, self.number_of_users

    def get_best_sat_id(self, mahimahi_ptr=None):
        best_sat_id = None
        best_sat_bw = 0

        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr

        for sat_id, sat_bw in self.cooked_bw.items():
            # Pass the previously connected satellite
            if sat_id == self.prev_sat_id:
                continue
            if best_sat_bw < sat_bw[mahimahi_ptr]:
                best_sat_id = sat_id
                best_sat_bw = sat_bw[mahimahi_ptr]
        return best_sat_id

    def get_runner_up_sat_id(self, mahimahi_ptr=None, method="holt-winter"):
        best_sat_id = None
        best_sat_bw = 0

        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr

        for sat_id, sat_bw in self.cooked_bw.items():
            target_sat_bw = None
            if sat_id == self.cur_sat_id:
                continue

            if method == "harmonic-mean":
                target_sat_bw = self.predict_bw(sat_id, mahimahi_ptr)
            elif method == "holt-winter":
                target_sat_bw = self.predict_bw_holt_winter(sat_id, mahimahi_ptr)
            else:
                print("Cannot happen")
                exit(1)

            assert(target_sat_bw is not None)
            # Pass the previously connected satellite
            if sat_id == self.prev_sat_id:
                continue
            if best_sat_bw < target_sat_bw:
                best_sat_id = sat_id
                best_sat_bw = target_sat_bw

        return best_sat_id, best_sat_bw

    def get_available_sats_id(self, mahimahi_ptr=None):
        sats_id = []
        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr
        for sat_id, sat_bw in self.cooked_bw.items():
            if sat_bw[mahimahi_ptr] > 0:
                sats_id.append(sat_id)
        return sats_id

    def handover_mpc_fast(self, cur_sat_id, mahimahi_ptr):
        harmonic_bw: dict[int:float] = {}
        is_handover = False

        # default assumes that this is the first request so error is 0 since we have never predicted bandwidth
        # past_download = float(video_chunk_size) / float(delay) / M_IN_K
        # Past BW
        pred_bw = self.predict_bw(cur_sat_id, mahimahi_ptr, robustness=False)
        # pred_download_bw = self.predict_download_bw(robustness=False)

        harmonic_bw[cur_sat_id] = pred_bw

        best_bw = harmonic_bw[cur_sat_id]
        best_sat_id = cur_sat_id

        # Past Download
        # Calculate the harmonic bw for all satellites
        # Find best satellite & bandwidth
        for sat_id, sat_bw in self.cooked_bw.items():
            if sat_id == cur_sat_id:
                continue
            else:
                # Check if it is visible now
                if self.cooked_bw[sat_id][mahimahi_ptr-1] != 0.0:
                    # Pass the previously connected satellite
                    if sat_id == self.prev_sat_id:
                        continue
                    # Predict the past bw for error estimates of MPC
                    for i in reversed(range(1, MPC_FUTURE_CHUNK_COUNT+1)):
                        self.predict_bw(sat_id, mahimahi_ptr - i)
                    harmonic_bw[sat_id] = self.predict_bw(sat_id, mahimahi_ptr, robustness=False)
                    harmonic_bw[sat_id] -= harmonic_bw[cur_sat_id] * HANDOVER_DELAY * HANDOVER_WEIGHT

                    if best_bw < harmonic_bw[sat_id]:
                        best_bw = harmonic_bw[sat_id]
                        best_sat_id = sat_id
                        is_handover = True

        if is_handover:
            self.prev_sat_id = best_sat_id

        return is_handover, best_sat_id

    def handover_greedy(self, cur_sat_id, mahimahi_ptr):
        harmonic_bw: dict[int:float] = {}
        is_handover = False

        # default assumes that this is the first request so error is 0 since we have never predicted bandwidth
        # past_download = float(video_chunk_size) / float(delay) / M_IN_K
        # Past BW
        # harmonic_bw[cur_sat_id] = self.predict_bw(cur_sat_id, mahimahi_ptr, robustness=True)
        harmonic_bw[cur_sat_id] = self.predict_bw_holt_winter(cur_sat_id, mahimahi_ptr)
        # pred_download_bw = self.predict_download_bw(robustness=True)
        # past_bw = self.cooked_bw[cur_sat_id][mahimahi_ptr - 1]

        best_bw = harmonic_bw[cur_sat_id]
        best_sat_id = cur_sat_id

        # Past Download
        # Calculate the harmonic bw for all satellites
        # Find best satellite & bandwidth
        for sat_id, sat_bw in self.cooked_bw.items():
            if sat_id == cur_sat_id:
                continue
            else:
                # Check if it is visible now
                if self.cooked_bw[sat_id][mahimahi_ptr-1] != 0.0 and self.cooked_bw[sat_id][mahimahi_ptr] != 0.0:
                    # Pass the previously connected satellite
                    if sat_id == self.prev_sat_id:
                        continue
                    """"
                    # Predict the past bw for error estimates of MPC
                    for i in reversed(range(1, MPC_FUTURE_CHUNK_COUNT+1)):
                        self.predict_bw(sat_id, mahimahi_ptr - i)
                    """
                    # harmonic_bw[sat_id] = self.predict_bw(sat_id, mahimahi_ptr, robustness=True)
                    harmonic_bw[sat_id] = self.predict_bw_holt_winter(sat_id, mahimahi_ptr)
                    harmonic_bw[sat_id] -= harmonic_bw[cur_sat_id] * HANDOVER_DELAY * HANDOVER_WEIGHT

                    if best_bw < harmonic_bw[sat_id]:
                        best_bw = harmonic_bw[sat_id]
                        best_sat_id = sat_id
                        is_handover = True

        if is_handover:
            self.prev_sat_id = best_sat_id

        return is_handover, best_sat_id

    def handover_mpc_greedy(self, cur_sat_id, mahimahi_ptr):
        is_handover = False

        # Without handover case, current SAT
        if self.cooked_bw[cur_sat_id][mahimahi_ptr] != 0.0:
            best_bit_rate_combo, best_reward, best_pred_bw \
                = self.calculate_greedy_mpc(cur_sat_id, mahimahi_ptr, robustness=True, method="holt-winter")
        else:
            best_bit_rate_combo = ()
            best_pred_bw = -10000
            best_reward = -10000

        best_sat_id = cur_sat_id

        # Past Download
        # Calculate the harmonic bw for all satellites
        # Find best satellite & bandwidth
        for sat_id, sat_bw in self.cooked_bw.items():
            if sat_id == cur_sat_id:
                continue
            else:
                # Check if it is visible now
                if self.cooked_bw[sat_id][mahimahi_ptr-1] != 0.0 and self.cooked_bw[sat_id][mahimahi_ptr] != 0.0:
                    # Pass the previously connected satellite
                    if sat_id == self.prev_sat_id:
                        continue

                    tmp_bit_rate_combo, tmp_reward, pred_bw \
                        = self.calculate_greedy_mpc(sat_id, mahimahi_ptr, cur_sat_id, robustness=True, handover=True,
                                                    method="holt-winter")
                    if best_reward < tmp_reward:
                        best_reward = tmp_reward
                        best_sat_id = sat_id
                        best_pred_bw = pred_bw
                        is_handover = True
                    elif best_reward == tmp_reward and best_pred_bw < pred_bw:
                        best_sat_id = sat_id
                        best_pred_bw = pred_bw
                        is_handover = True

        if is_handover:
            self.prev_sat_id = best_sat_id

        return is_handover, best_sat_id

    def handover_qoe_v2(self, cur_sat_id, mahimahi_ptr, only_runner_up=True):
        is_handover = False
        best_sat_id = cur_sat_id

        ho_sat_id, ho_stamp, best_combo, max_reward = self.calculate_mpc_with_handover(cur_sat_id, mahimahi_ptr,
                                                                                       only_runner_up=only_runner_up)
        # print(cur_sat_id, ho_sat_id, ho_stamp, best_combo, max_reward)
        if ho_stamp == 0:
            is_handover = True
            best_sat_id = ho_sat_id

        return is_handover, best_sat_id

    def handover_mpc_truth(self, cur_sat_id, mahimahi_ptr):
        harmonic_bw: dict[int:float] = {}
        is_handover = False

        # default assumes that this is the first request so error is 0 since we have never predicted bandwidth
        # past_download = float(video_chunk_size) / float(delay) / M_IN_K
        # Past BW
        # pred_bw = self.predict_bw(cur_sat_id, mahimahi_ptr + 1, robustness=True)
        # pred_download_bw = self.predict_download_bw(robustness=True)
        harmonic_bw[cur_sat_id] = sum(self.cooked_bw[cur_sat_id][mahimahi_ptr: mahimahi_ptr+MPC_FUTURE_CHUNK_COUNT]) \
                                  / MPC_FUTURE_CHUNK_COUNT / self.number_of_users

        best_bw = harmonic_bw[cur_sat_id]
        best_sat_id = cur_sat_id

        # Past Download
        # Calculate the harmonic bw for all satellites
        # Find best satellite & bandwidth
        for sat_id, sat_bw in self.cooked_bw.items():
            if sat_id == cur_sat_id:
                continue
            else:
                # Check if it is visible now
                if 0.0 not in self.cooked_bw[sat_id][mahimahi_ptr: mahimahi_ptr + 5]:
                    # Pass the previously connected satellite
                    if sat_id == self.prev_sat_id:
                        continue

                    harmonic_bw[sat_id] = sum(self.cooked_bw[sat_id][mahimahi_ptr: mahimahi_ptr+MPC_FUTURE_CHUNK_COUNT]) \
                                          / MPC_FUTURE_CHUNK_COUNT / self.number_of_users
                    harmonic_bw[sat_id] -= harmonic_bw[cur_sat_id] * HANDOVER_DELAY * HANDOVER_WEIGHT

                    if best_bw < harmonic_bw[sat_id]:
                        best_bw = harmonic_bw[sat_id]
                        best_sat_id = sat_id
                        is_handover = True

        if is_handover:
            self.prev_sat_id = best_sat_id

        return is_handover, best_sat_id

    def handover_naive(self, cur_sat_id=None, mahimahi_ptr=None):
        if cur_sat_id is None:
            cur_sat_id = self.cur_sat_id
        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr

        if self.cooked_bw[cur_sat_id][mahimahi_ptr] == 0.0:
            next_sat_id = self.get_best_sat_id(mahimahi_ptr)
            new_sat_id = next_sat_id

            self.prev_sat_id = next_sat_id
            return True, new_sat_id
        else:
            return False, None

    def snapshot_virtual_vars(self):
        self.virtual_mahimahi_ptr = self.mahimahi_ptr
        self.virtual_last_mahimahi_time = self.last_mahimahi_time
        self.virtual_cur_sat_id = self.cur_sat_id

    def predict_bw(self, cur_sat_id, mahimahi_ptr, robustness=True):
        curr_error = 0

        # past_bw = self.cooked_bw[self.cur_sat_id][self.mahimahi_ptr - 1]
        past_bw = self.cooked_bw[cur_sat_id][mahimahi_ptr - 1] / self.number_of_users
        if past_bw == 0:
            return 0

        if cur_sat_id in self.past_bw_ests.keys() and len(self.past_bw_ests[cur_sat_id]) > 0 \
                and mahimahi_ptr - 1 in self.past_bw_ests[cur_sat_id].keys():
            curr_error = abs(self.past_bw_ests[cur_sat_id][mahimahi_ptr - 1] - past_bw) / float(past_bw)
        if cur_sat_id not in self.past_bw_errors.keys():
            self.past_bw_errors[cur_sat_id] = []
        self.past_bw_errors[cur_sat_id].append(curr_error)

        # pick bitrate according to MPC
        # first get harmonic mean of last 5 bandwidths
        start_index = mahimahi_ptr - MPC_FUTURE_CHUNK_COUNT
        if start_index < 0:
            start_index = 0

        past_bws = []
        for tmp_bw in self.cooked_bw[cur_sat_id][start_index: mahimahi_ptr]:
            past_bws.append(tmp_bw / self.number_of_users)

        # Newly possible satellite case
        if all(v == 0.0 for v in past_bws):
            return self.cooked_bw[cur_sat_id][mahimahi_ptr] / self.number_of_users

        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        bandwidth_sum = 0
        for past_val in past_bws:
            bandwidth_sum += (1 / float(past_val))

        harmonic_bw = 1.0 / (bandwidth_sum / len(past_bws))
        if cur_sat_id not in self.past_bw_ests.keys():
            self.past_bw_ests[cur_sat_id] = {}
        if mahimahi_ptr not in self.past_bw_ests[cur_sat_id].keys():
            self.past_bw_ests[cur_sat_id][mahimahi_ptr] = None
        self.past_bw_ests[cur_sat_id][mahimahi_ptr] = harmonic_bw

        if robustness:
            # future bandwidth prediction
            # divide by 1 + max of last 5 (or up to 5) errors
            error_pos = -MPC_FUTURE_CHUNK_COUNT
            if cur_sat_id in self.past_bw_errors.keys() and len(
                    self.past_bw_errors[cur_sat_id]) < MPC_FUTURE_CHUNK_COUNT:
                error_pos = -len(self.past_bw_errors[cur_sat_id])
            max_error = float(max(self.past_bw_errors[cur_sat_id][error_pos:]))
            harmonic_bw = harmonic_bw / (1 + max_error)  # robustMPC here

        return harmonic_bw

    def predict_download_bw(self, robustness=False):
        curr_error = 0

        past_download_bw = self.download_bw[-1]
        if len(self.past_download_ests) > 0:
            curr_error = abs(self.past_download_ests[-1] - past_download_bw) / float(past_download_bw)
        self.past_download_bw_errors.append(curr_error)

        # pick bitrate according to MPC
        # first get harmonic mean of last 5 bandwidths
        # past_bws = self.cooked_bw[self.cur_sat_id][start_index: self.mahimahi_ptr]
        past_bws = self.download_bw[-MPC_FUTURE_CHUNK_COUNT:]
        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        bandwidth_sum = 0
        for past_val in past_bws:
            bandwidth_sum += (1 / float(past_val))

        harmonic_bw = 1.0 / (bandwidth_sum / len(past_bws))
        self.past_download_ests.append(harmonic_bw)

        if robustness:
            # future bandwidth prediction
            # divide by 1 + max of last 5 (or up to 5) errors
            error_pos = -MPC_FUTURE_CHUNK_COUNT
            if len(self.past_download_bw_errors) < MPC_FUTURE_CHUNK_COUNT:
                error_pos = -len(self.past_download_bw_errors)
            max_error = float(max(self.past_download_bw_errors[error_pos:]))
            harmonic_bw = harmonic_bw / (1 + max_error)  # robustMPC here

        return harmonic_bw

    def calculate_greedy_mpc(self, new_sat_id, mahimahi_ptr, cur_sat_id=None, combo=None, handover=False,
                             robustness=True, method="holt-winter"):
        harmonic_bw = None
        if method == "harmonic-mean":
            harmonic_bw = self.predict_bw(new_sat_id, mahimahi_ptr, robustness)
        elif method == "holt-winter":
            harmonic_bw = self.predict_bw_holt_winter(new_sat_id, mahimahi_ptr)
        else:
            print("Cannot happen")
            exit(1)

        assert(harmonic_bw is not None)
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = self.video_len - self.video_chunk_counter
        last_index = self.get_total_video_chunk() - video_chunk_remain

        chunk_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(BIT_RATE_LEVELS)), repeat=MPC_FUTURE_CHUNK_COUNT):
            chunk_combo_option.append(combo)

        future_chunk_length = MPC_FUTURE_CHUNK_COUNT
        if video_chunk_remain < MPC_FUTURE_CHUNK_COUNT:
            future_chunk_length = video_chunk_remain

        # all possible combinations of 5 chunk bitrates for 6 bitrate options (6^5 options)
        # iterate over list and for each, compute reward and store max reward combination
        max_reward = -10000000
        best_combo = ()
        start_buffer = self.buffer_size / MILLI_IN_SECOND

        if combo:
            chunk_combo_option = [combo]

        for full_combo in chunk_combo_option:
            # Break at the end of the chunk
            if future_chunk_length == 0:
                send_data = self.last_quality
                break
            combo = full_combo[0: future_chunk_length]
            # calculate total rebuffer time for this combination (start with start_buffer and subtract
            # each download time and add 2 seconds in that order)
            curr_rebuffer_time = 0
            curr_buffer = start_buffer

            bitrate_sum = 0
            smoothness_diffs = 0
            last_quality = self.last_quality
            for position in range(0, len(combo)):
                chunk_quality = combo[position]
                index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                download_time = (self.get_video_size(chunk_quality, index) / B_IN_MB) \
                                / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                if handover:
                    download_time += HANDOVER_DELAY

                if curr_buffer < download_time:
                    curr_rebuffer_time += (download_time - curr_buffer)
                    curr_buffer = 0.0
                else:
                    curr_buffer -= download_time
                curr_buffer += self.video_chunk_len / MILLI_IN_SECOND

                # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                bitrate_sum += self.video_bit_rate[chunk_quality]
                smoothness_diffs += abs(self.video_bit_rate[chunk_quality] - self.video_bit_rate[last_quality])
                last_quality = chunk_quality
            # compute reward for this combination (one reward per 5-chunk combo)
            # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

            # reward = (bitrate_sum / 1000.) - (REBUF_PENALTY * curr_rebuffer_time) - (smoothness_diffs / 1000.)
            # 10~140 - 0~100 - 0~130
            reward = bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                     - SMOOTH_PENALTY * smoothness_diffs / M_IN_K
            if reward > max_reward:
                best_combo = combo
                max_reward = reward
            elif reward == max_reward and sum(combo) > sum(best_combo):
                best_combo = combo
                max_reward = reward

        return best_combo, max_reward, harmonic_bw

    def calculate_mpc_with_handover(self, cur_sat_id, mahimahi_ptr, robustness=True, only_runner_up=True,
                                    method="holt-winter"):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = self.video_len - self.video_chunk_counter
        last_index = self.get_total_video_chunk() - video_chunk_remain

        chunk_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(BIT_RATE_LEVELS)), repeat=MPC_FUTURE_CHUNK_COUNT):
            chunk_combo_option.append(combo)

        future_chunk_length = MPC_FUTURE_CHUNK_COUNT
        if video_chunk_remain < MPC_FUTURE_CHUNK_COUNT:
            future_chunk_length = video_chunk_remain

        max_reward = -10000000
        best_combo = ()
        ho_sat_id = cur_sat_id
        ho_stamp = -1
        cur_harmonic_bw, runner_up_sat_id = None, None
        if method == "harmonic-mean":
            cur_harmonic_bw = self.predict_bw(cur_sat_id, mahimahi_ptr, robustness)
            runner_up_sat_id, _ = self.get_runner_up_sat_id(mahimahi_ptr, method="harmonic-mean")
        elif method == "holt-winter":
            cur_harmonic_bw = self.predict_bw_holt_winter(cur_sat_id, mahimahi_ptr)
            runner_up_sat_id, _ = self.get_runner_up_sat_id(mahimahi_ptr, method="holt-winter")
        else:
            print("Cannot happen")
            exit(1)
        start_buffer = self.buffer_size / MILLI_IN_SECOND

        if future_chunk_length == 0:
            return ho_sat_id, ho_stamp, best_combo, max_reward

        for next_sat_id, next_sat_bw in self.cooked_bw.items():
            if next_sat_id == cur_sat_id:
                continue
            else:
                # Check if it is visible now
                if self.cooked_bw[next_sat_id][mahimahi_ptr - 1] != 0.0 and self.cooked_bw[next_sat_id][mahimahi_ptr] != 0.0:
                    # Pass the previously connected satellite
                    if next_sat_id == self.prev_sat_id:
                        continue

                    if only_runner_up and runner_up_sat_id != next_sat_id:
                        # Only consider the next-best satellite
                        continue

                    # Based on the bw, not download bw
                    next_harmonic_bw = None
                    if method == "harmonic-mean":
                        next_harmonic_bw = self.predict_bw(next_sat_id, mahimahi_ptr, robustness)
                    elif method == "holt-winter":
                        next_harmonic_bw = self.predict_bw_holt_winter(next_sat_id, mahimahi_ptr)
                    else:
                        print("Cannot happen")
                        exit(1)

                    for ho_index in range(MPC_FUTURE_CHUNK_COUNT+1):
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
                            last_quality = self.last_quality
                            is_impossible = False

                            for position in range(0, len(combo)):
                                chunk_quality = combo[position]
                                index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                                download_time = 0
                                if ho_index > position:
                                    harmonic_bw = cur_harmonic_bw

                                elif ho_index == position:
                                    harmonic_bw = next_harmonic_bw
                                    # Give them a penalty
                                    download_time += HANDOVER_DELAY
                                else:
                                    harmonic_bw = next_harmonic_bw

                                download_time += (self.video_size[chunk_quality][index]self.get_video_size(chunk_quality, index) / B_IN_MB) \
                                                / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                                if curr_buffer < download_time:
                                    curr_rebuffer_time += (download_time - curr_buffer)
                                    curr_buffer = 0.0
                                else:
                                    curr_buffer -= download_time
                                curr_buffer += self.video_chunk_len / MILLI_IN_SECOND

                                # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                                # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                                bitrate_sum += self.video_bit_rate[chunk_quality]
                                smoothness_diffs += abs(
                                    self.video_bit_rate[chunk_quality] - self.video_bit_rate[last_quality])
                                last_quality = chunk_quality
                            # compute reward for this combination (one reward per 5-chunk combo)

                            # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

                            # 10~140 - 0~100 - 0~130
                            reward = bitrate_sum * QUALITY_FACTOR - (REBUF_PENALTY * curr_rebuffer_time) \
                                     - SMOOTH_PENALTY * smoothness_diffs

                            if reward > max_reward:
                                best_combo = combo
                                max_reward = reward
                                ho_sat_id = next_sat_id
                                ho_stamp = ho_index
                            elif reward == max_reward and (sum(combo) > sum(best_combo) or ho_index == MPC_FUTURE_CHUNK_COUNT):
                                best_combo = combo
                                max_reward = reward
                                ho_sat_id = next_sat_id
                                ho_stamp = ho_index

        return ho_sat_id, ho_stamp, best_combo, max_reward

    def get_truth_download_time(self, video_chunk_size, handover_type):
        # use the delivery opportunity in mahimahi
        delay = 0.0  # in ms
        video_chunk_counter_sent = 0  # in bytes

        # past_download_bw = self.download_bw[-1]
        # self.download_bw.append(float(video_chunk_size) / float(delay) / M_IN_K)
        while True:  # download video chunk over mahimahi
            throughput = self.cooked_bw[self.virtual_cur_sat_id][self.virtual_mahimahi_ptr] * B_IN_MB / BITS_IN_BYTE
            assert throughput != 0.0

            duration = self.cooked_time[self.virtual_mahimahi_ptr] \
                       - self.virtual_last_mahimahi_time
            packet_payload = throughput * duration * PACKET_PAYLOAD_PORTION

            if video_chunk_counter_sent + packet_payload > video_chunk_size:
                fractional_time = (video_chunk_size - video_chunk_counter_sent) / \
                                  throughput / PACKET_PAYLOAD_PORTION
                delay += fractional_time
                self.virtual_last_mahimahi_time += fractional_time
                break

            video_chunk_counter_sent += packet_payload
            delay += duration

            self.virtual_last_mahimahi_time = self.cooked_time[self.virtual_mahimahi_ptr]
            self.virtual_mahimahi_ptr += 1

            if self.virtual_mahimahi_ptr >= len(self.cooked_bw[self.virtual_cur_sat_id]):
                break

            # Check Handover
            handover_result, new_sat_id = self.check_handover(handover_type, self.virtual_cur_sat_id
                                                              , self.virtual_mahimahi_ptr)
            if handover_result:
                delay += HANDOVER_DELAY
                self.virtual_cur_sat_id = new_sat_id

        delay *= MILLI_IN_SECOND
        delay += LINK_RTT

        # add a multiplicative noise to the delay
        # delay *= np.random.uniform(NOISE_LOW, NOISE_HIGH)

        # download_bw = video_chunk_size / float(delay) / M_IN_K
        # video_chunk_size / B_IN_MB / download_bw
        return float(delay) / M_IN_K

    def get_video_chunk_counter(self):
        return self.video_chunk_counter

    def get_total_video_chunk(self):
        return self.video_len

    def get_buffer_size(self) -> float:
        return self.buffer_size

    def predict_future_bw(self, method="holt-winter", robustness=True):
        # harmonic_bw: dict[int:float] = {}
        pred_bw = None
        pred_download_bw = None
        if method == "holt-winter":
            pred_bw = self.predict_bw_holt_winter(self.cur_sat_id, self.mahimahi_ptr)
            pred_download_bw = self.predict_download_bw_holt_winter()
        elif method == "harmonic":
            if robustness:
                pred_bw = self.predict_bw(self.cur_sat_id, self.mahimahi_ptr, robustness=True)
                pred_download_bw = self.predict_download_bw(robustness=True)
                # harmonic_bw[self.cur_sat_id] = pred_bw
                # self.harmonic_bw = harmonic_bw
            else:
                pred_bw = self.predict_bw(self.cur_sat_id, self.mahimahi_ptr, robustness=False)
                pred_download_bw = self.predict_download_bw(robustness=False)
                # harmonic_bw[self.cur_sat_id] = pred_bw
                # self.harmonic_bw = harmonic_bw
        else:
            print("Cannot happen")
            exit(1)
        return pred_bw, pred_download_bw

    def predict_download_bw_holt_winter(self, m=172):
        cur_sat_past_list = self.download_bw
        if len(cur_sat_past_list) <= 1:
            return self.download_bw[-1]

        cur_sat_past_bws = pd.Series(cur_sat_past_list)
        cur_sat_past_bws.index.freq = 's'

        # alpha = 1 / (2 * m)
        fitted_model = ExponentialSmoothing(cur_sat_past_bws, trend='add').fit()
        # fitted_model = ExponentialSmoothing(cur_sat_past_bws, trend='mul').fit()

        # fitted_model = ExponentialSmoothing(cur_sat_past_bws
        # test_predictions = fitted_model.forecast(5)
        test_predictions = fitted_model.forecast(1)

        pred_bw = sum(test_predictions) / len(test_predictions)

        return pred_bw

    def predict_bw_holt_winter(self, cur_sat_id, mahimahi_ptr, m=172):
        start_index = mahimahi_ptr - MPC_FUTURE_CHUNK_COUNT
        if start_index < 0:
            start_index = 0
        cur_sat_past_list = [item / self.number_of_users for item
                             in self.cooked_bw[cur_sat_id][start_index:start_index+MPC_FUTURE_CHUNK_COUNT]]
        while len(cur_sat_past_list) != 0 and cur_sat_past_list[0] == 0.0:
            cur_sat_past_list = cur_sat_past_list[1:]

        if len(cur_sat_past_list) <= 1 or any(v == 0 for v in cur_sat_past_list):
            # Just past bw
            return self.cooked_bw[cur_sat_id][mahimahi_ptr-1] / self.number_of_users

        cur_sat_past_bws = pd.Series(cur_sat_past_list)
        cur_sat_past_bws.index.freq = 's'

        # alpha = 1 / (2 * m)
        fitted_model = ExponentialSmoothing(cur_sat_past_bws, trend='add').fit()
        # fitted_model = ExponentialSmoothing(cur_sat_past_bws, trend='mul').fit()

        # fitted_model = ExponentialSmoothing(cur_sat_past_bws
        # test_predictions = fitted_model.forecast(5)
        test_predictions = fitted_model.forecast(1)

        pred_bw = sum(test_predictions) / len(test_predictions)
        return pred_bw

    def get_video_size(self, chunk_quality, index) -> int:
        return self.video_size[chunk_quality][index]

    def update_number_of_users(self):
        diff_num = np.random.randint(-3, 3)
        if diff_num == -3:
            self.number_of_users -= 1
        elif diff_num == 2:
            self.number_of_users += 1

        if self.number_of_users < 10:
            self.number_of_users = 10
        elif self.number_of_users > 50:
            self.number_of_users = 50

        return self.number_of_users

    def check_handover(self, handover_type, cur_sat_id=None, mahimahi_ptr=None):
        # Check Handover
        is_handover = False

        if cur_sat_id is None:
            cur_sat_id = self.cur_sat_id

        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr

        new_sat_id = cur_sat_id
        if handover_type == "naive":
            is_handover, new_sat_id = self.handover_naive(cur_sat_id, mahimahi_ptr)
        elif handover_type == "truth-mpc":
            is_handover, new_sat_id = self.handover_mpc_truth(cur_sat_id, mahimahi_ptr)
        elif handover_type == "greedy":
            is_handover, new_sat_id = self.handover_greedy(cur_sat_id, mahimahi_ptr)
        elif handover_type == "mpc-greedy":
            is_handover, new_sat_id = self.handover_mpc_greedy(cur_sat_id, mahimahi_ptr)
        elif handover_type == "QoE-all":
            is_handover, new_sat_id = self.handover_qoe_v2(cur_sat_id, mahimahi_ptr, only_runner_up=False)
        elif handover_type == "QoE-pruned":
            is_handover, new_sat_id = self.handover_qoe_v2(cur_sat_id, mahimahi_ptr, only_runner_up=True)
        else:
            print("Cannot happen!")
            exit(-1)
        return is_handover, new_sat_id



