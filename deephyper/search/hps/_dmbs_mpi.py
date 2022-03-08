import enum
import logging
import os
import pathlib
import signal
import time

import numpy as np
import pandas as pd
import skopt

from mpi4py import MPI
from deephyper.core.exceptions import SearchTerminationError
from sklearn.ensemble import GradientBoostingRegressor

TERMINATION = 10


class History:
    """History"""

    def __init__(self) -> None:
        self._list_x = []  # vector of hyperparameters
        self._list_y = []  # objective values
        self._keys_infos = []  # keys
        self._list_infos = []  # values
        self.n_buffered = 0

    def append_keys_infos(self, k: list):
        self._keys_infos.extend(k)

    def get_keys_infos(self) -> list:
        return self._keys_infos

    def append(self, x, y, infos):
        self._list_x.append(x)
        self._list_y.append(y)
        self._list_infos.append(infos)
        self.n_buffered += 1

    def extend(self, x: list, y: list, infos: dict):
        self._list_x.extend(x)
        self._list_y.extend(y)

        infos = np.array([v for v in infos.values()], dtype="O").T.tolist()
        self._list_infos.extend(infos)
        self.n_buffered += len(x)

    def length(self):
        return len(self._list_x)

    def value(self):
        return self._list_x[:], self._list_y[:]

    def infos(self, k=None):
        list_infos = np.array(self._list_infos, dtype="O").T
        if k is not None:
            infos = {k: v[-k:] for k, v in zip(self._keys_infos, list_infos)}
            return self._list_x[-k:], self._list_y[-k], infos
        else:
            infos = {k: v for k, v in zip(self._keys_infos, list_infos)}
            return self._list_x, self._list_y, infos

    def reset_buffer(self):
        self.n_buffered = 0


class DMBSMPI:
    """Distributed Model-Based Search based on the `Scikit-Optimized Optimizer <https://scikit-optimize.github.io/stable/modules/generated/skopt.Optimizer.html#skopt.Optimizer>`_.

    Args:
        problem (HpProblem): Hyperparameter problem describing the search space to explore.
        evaluator (Evaluator): An ``Evaluator`` instance responsible of distributing the tasks.
        random_state (int, optional): Random seed. Defaults to ``None``.
        log_dir (str, optional): Log directory where search's results are saved. Defaults to ``"."``.
        verbose (int, optional): Indicate the verbosity level of the search. Defaults to ``0``.
        comm (optional): .... Defaults to ``None``.
        run_function_kwargs (dict): .... Defaults to ``None``.
        n_jobs (int, optional): .... Defaults to ``1``.
        surrogate_model (str, optional): .... Defaults to ``"RF"``.
        lazy_socket_allocation (bool, optional): .... Defaults to ``True``.
        sync_communication (bool, optional): Force workers to communicate synchronously. Defaults to ``False``.
        sync_communication_freq (int, optional): Manage the frequency at which workers should communicate their results. Defaults to ``10``.
    """

    def __init__(
        self,
        problem,
        run_function,
        random_state: int = None,
        log_dir: str = ".",
        verbose: int = 0,
        comm=None,
        run_function_kwargs: dict = None,
        n_jobs: int = 1,
        surrogate_model: str = "RF",
        n_initial_points: int = 10,
        lazy_socket_allocation: bool = True,
        sync_communication: bool = False,
        sync_communication_freq: int = 10,
    ):

        self._problem = problem
        self._run_function = run_function
        self._run_function_kwargs = (
            {} if run_function_kwargs is None else run_function_kwargs
        )

        if type(random_state) is int:
            self._seed = random_state
            self._random_state = np.random.RandomState(random_state)
        elif isinstance(random_state, np.random.RandomState):
            self._random_state = random_state
        else:
            self._random_state = np.random.RandomState()

        # Create logging directory if does not exist
        self._log_dir = os.path.abspath(log_dir)
        pathlib.Path(log_dir).mkdir(parents=False, exist_ok=True)

        self._verbose = verbose

        # mpi
        self._comm = comm if comm else MPI.COMM_WORLD
        self._rank = self._comm.Get_rank()
        self._size = self._comm.Get_size()
        logging.info(f"DMBSMPI has {self._size} worker(s)")

        # force socket allocation with dummy message to reduce overhead
        if not lazy_socket_allocation:
            logging.info("Initializing communication...")
            ti = time.time()
            logging.info("Sending to all...")
            t1 = time.time()
            req_send = [
                self._comm.isend(None, dest=i)
                for i in range(self._size)
                if i != self._rank
            ]
            MPI.Request.waitall(req_send)
            logging.info(f"Sending to all done in {time.time() - t1:.4f} sec.")

            logging.info("Receiving from all...")
            t1 = time.time()
            req_recv = [
                self._comm.irecv(source=i) for i in range(self._size) if i != self._rank
            ]
            MPI.Request.waitall(req_recv)
            logging.info(f"Receiving from all done in {time.time() - t1:.4f} sec.")
            logging.info(
                f"Initializing communications done in {time.time() - ti:.4f} sec."
            )

        self._sync_communication = sync_communication
        self._sync_communication_freq = sync_communication_freq

        # set random state for given rank
        self._rank_seed = self._random_state.randint(
            low=0, high=2**32, size=self._size
        )[self._rank]

        self._timestamp = time.time()

        self._history = History()
        self._opt = None
        self._opt_space = self._problem.space
        self._opt_kwargs = dict(
            dimensions=self._opt_space,
            base_estimator=self._get_surrogate_model(
                surrogate_model,
                n_jobs,
                random_state=self._rank_seed,
            ),
            acq_func="LCB",
            acq_optimizer="boltzmann_sampling",
            acq_optimizer_kwargs={
                "n_points": 10000,
                "boltzmann_gamma": 1,
                "n_jobs": n_jobs,
            },
            n_initial_points=n_initial_points,
            random_state=self._rank_seed,
        )

    def send_all(self, x, y, infos):
        logging.info("Sending to all...")
        t1 = time.time()

        data = (x, y, infos)
        req_send = [
            self._comm.isend(data, dest=i) for i in range(self._size) if i != self._rank
        ]
        MPI.Request.waitall(req_send)

        logging.info(f"Sending to all done in {time.time() - t1:.4f} sec.")

    def send_all_termination(self):
        logging.info("Sending termination code to all...")
        t1 = time.time()

        req_send = [
            self._comm.isend(TERMINATION, dest=i)
            for i in range(self._size)
            if i != self._rank
        ]
        MPI.Request.waitall(req_send)

        logging.info(
            f"Sending termination code to all done in {time.time() - t1:.4f} sec."
        )

    def recv_any(self):
        logging.info("Receiving from any...")
        t1 = time.time()

        n_received = 0
        received_any = self._size > 1

        while received_any:

            received_any = False
            req_recv = [
                self._comm.irecv(source=i) for i in range(self._size) if i != self._rank
            ]

            # asynchronous
            for req in req_recv:
                done, data = req.test()
                if done:
                    if data != TERMINATION:
                        received_any = True
                        n_received += 1
                        x, y, infos = data
                        self._history.append(x, y, infos)
                else:
                    req.cancel()

        logging.info(
            f"Received {n_received} configurations in {time.time() - t1:.4f} sec."
        )

    def broadcast(self, X: list, Y: list, infos: list):
        logging.info("Broadcasting to all...")
        t1 = time.time()
        data = self._comm.allgather((X, Y, infos))

        for i, (X, Y, infos) in enumerate(data):
            if i != self._rank:
                self._history.extend(X, Y, infos)
        n_received = (len(data) - 1) * data[0]
        logging.info(
            f"Broadcast received {n_received} configurations in {time.time() - t1:.4f} sec."
        )
        
    def terminate(self):
        """Terminate the search.

        Raises:
            SearchTerminationError: raised when the search is terminated with SIGALARM
        """
        logging.info("Search is being stopped!")

        raise SearchTerminationError

    def _set_timeout(self, timeout=None):
        def handler(signum, frame):
            self.terminate()

        signal.signal(signal.SIGALRM, handler)

        if np.isscalar(timeout) and timeout > 0:
            signal.alarm(timeout)

    def search(self, max_evals: int = -1, timeout: int = None):
        """Execute the search algorithm.

        Args:
            max_evals (int, optional): The maximum number of evaluations of the run function to perform before stopping the search. Defaults to ``-1``, will run indefinitely.
            timeout (int, optional): The time budget (in seconds) of the search before stopping. Defaults to ``None``, will not impose a time budget.

        Returns:
            DataFrame: a pandas DataFrame containing the evaluations performed.
        """
        if timeout is not None:
            if type(timeout) is not int:
                raise ValueError(
                    f"'timeout' shoud be of type'int' but is of type '{type(timeout)}'!"
                )
            if timeout <= 0:
                raise ValueError(f"'timeout' should be > 0!")

        self._set_timeout(timeout)

        try:
            self._search(max_evals, timeout)
        except SearchTerminationError:
            self.send_all_termination()

        if self._rank == 0:
            path_results = os.path.join(self._log_dir, "results.csv")
            self.recv_any()
            results = self.gather_results()
            results.to_csv(path_results)
            return results
        else:
            return None

    def _setup_optimizer(self):
        # if self._fitted:
        #     self._opt_kwargs["n_initial_points"] = 0
        self._opt = skopt.Optimizer(**self._opt_kwargs)

    def _search(self, max_evals, timeout):

        if self._opt is None:
            self._setup_optimizer()

        logging.info("Asking 1 configuration...")
        t1 = time.time()
        x = self._opt.ask()
        logging.info(f"Asking took {time.time() - t1:.4f} sec.")

        logging.info("Executing the run-function...")
        t1 = time.time()
        y = self._run_function(self.to_dict(x), **self._run_function_kwargs)
        logging.info(f"Execution took {time.time() - t1:.4f} sec.")

        infos = [self._rank]
        self._history.append_keys_infos(["worker_rank"])

        # code to manage the @profile decorator
        profile_keys = ["objective", "timestamp_start", "timestamp_end"]
        if isinstance(y, dict) and all(k in y for k in profile_keys):
            profile = y
            y = profile["objective"]
            timestamp_start = profile["timestamp_start"] - self._timestamp
            timestamp_end = profile["timestamp_end"] - self._timestamp
            infos.extend([timestamp_start, timestamp_end])

            self._history.append_keys_infos(profile_keys[1:])

        y = -y  #! we do maximization

        self._history.append(x, y, infos)

        if self._sync_communication:
            if self._history.n_buffered % self._sync_communication_freq == 0:
                self.broadcast(*self._history.infos(k=self._history.n_buffered))
                self._history.reset_buffer()
        else:
            self.send_all(x, y, infos)
            self._history.reset_buffer()

        while max_evals < 0 or self._history.length() < max_evals:

            # collect x, y from other nodes (history)
            if not(self._sync_communication):
                self.recv_any()

            hist_X, hist_y = self._history.value()
            n_new = len(hist_X) - len(self._opt.Xi)

            logging.info("Fitting the optimizer...")
            t1 = time.time()
            self._opt.tell(hist_X[-n_new:], hist_y[-n_new:])
            logging.info(f"Fitting took {time.time() - t1:.4f} sec.")

            # ask next configuration
            logging.info("Asking 1 configuration...")
            t1 = time.time()
            x = self._opt.ask()
            logging.info(f"Asking took {time.time() - t1:.4f} sec.")

            logging.info("Executing the run-function...")
            t1 = time.time()
            y = self._run_function(self.to_dict(x), **self._run_function_kwargs)
            logging.info(f"Execution took {time.time() - t1:.4f} sec.")
            infos = [self._rank]

            # code to manage the profile decorator
            profile_keys = ["objective", "timestamp_start", "timestamp_end"]
            if isinstance(y, dict) and all(k in y for k in profile_keys):
                profile = y
                y = profile["objective"]
                timestamp_start = profile["timestamp_start"] - self._timestamp
                timestamp_end = profile["timestamp_end"] - self._timestamp
                infos.extend([timestamp_start, timestamp_end])

            y = -y  #! we do maximization

            # update shared history
            self._history.append(x, y, infos)

            if self._sync_communication:
                if self._history.n_buffered % self._sync_communication_freq == 0:
                    self.broadcast(*self._history.infos(k=self._history.n_buffered))
                    self._history.reset_buffer()
            else:
                self.send_all(x, y, infos)
                self._history.reset_buffer()

    def to_dict(self, x: list) -> dict:
        """Transform a list of hyperparameter values to a ``dict`` where keys are hyperparameters names and values are hyperparameters values.

        :meta private:

        Args:
            x (list): a list of hyperparameter values.

        Returns:
            dict: a dictionnary of hyperparameter names and values.
        """
        res = {}
        hps_names = self._problem.hyperparameter_names
        for i in range(len(x)):
            res[hps_names[i]] = x[i]
        return res

    def gather_results(self):
        x_list, y_list, infos_dict = self._history.infos()
        x_list = np.transpose(np.array(x_list))
        y_list = -np.array(y_list)

        results = {
            hp_name: x_list[i]
            for i, hp_name in enumerate(self._problem.hyperparameter_names)
        }
        results.update(dict(objective=y_list, **infos_dict))

        results = pd.DataFrame(data=results, index=list(range(len(y_list))))
        return results

    def _get_surrogate_model(
        self, name: str, n_jobs: int = None, random_state: int = None
    ):
        """Get a surrogate model from Scikit-Optimize.

        Args:
            name (str): name of the surrogate model.
            n_jobs (int): number of parallel processes to distribute the computation of the surrogate model.

        Raises:
            ValueError: when the name of the surrogate model is unknown.
        """
        accepted_names = ["RF", "ET", "GBRT", "DUMMY", "GP"]
        if not (name in accepted_names):
            raise ValueError(
                f"Unknown surrogate model {name}, please choose among {accepted_names}."
            )

        if name == "RF":
            surrogate = skopt.learning.RandomForestRegressor(
                n_estimators=100,
                min_samples_leaf=3,
                n_jobs=n_jobs,
                random_state=random_state,
            )
        elif name == "ET":
            surrogate = skopt.learning.ExtraTreesRegressor(
                n_estimators=100,
                min_samples_leaf=3,
                n_jobs=n_jobs,
                random_state=random_state,
            )
        elif name == "GBRT":

            gbrt = GradientBoostingRegressor(n_estimators=30, loss="quantile")
            surrogate = skopt.learning.GradientBoostingQuantileRegressor(
                base_estimator=gbrt, n_jobs=n_jobs, random_state=random_state
            )
        else:  # for DUMMY and GP
            surrogate = name

        return surrogate
