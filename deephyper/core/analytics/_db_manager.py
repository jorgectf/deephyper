import json
import os
import abc
import getpass
import subprocess

import pandas as pd
import yaml
from datetime import datetime
from tinydb import TinyDB, Query


class DBManager(abc.ABC):
    """Database Manager, for saving DeepHyper experiments and accessing/modifying the resulting databases.

    Example Usage:

        >>> dbm = DBManager(user_name="Bob", path="path/to/db.json")

    Args:
        user_name (str, optional): the name of the user accessing the database. Defaults to ``os.getlogin()``.
        path (str, optional): the path to the database. Defaults to ``"~/.deephyper/local_db.json"``.
    """

    def __init__(
        self,
        user_name: str = None,
        path: str = "~/.deephyper/local_db.json",
    ) -> None:
        self._user_name = user_name if user_name else getpass.getuser()
        self._db = TinyDB(path)

    def _get_pip_env(self, pip_versions=True):
        if isinstance(pip_versions, str):
            with open(pip_versions, "r") as file:
                pip_env = json.load(file)
        elif pip_versions:
            pip_list_com = subprocess.run(
                ["pip", "list", "--format", "json"], stdout=subprocess.PIPE
            )
            pip_env = json.loads(pip_list_com.stdout.decode("utf-8"))
        else:
            return None
        return pip_env

    def add(
        self,
        log_dir: str,
        label: str = None,
        description: str = None,
        pip_versions: str or bool = True,
        metadata: dict = None,
    ):
        """Adds an experiment to the database.

        Example Usage:

            >>> dbm = DBManager(user_name="Bob", path="path/to/db.json")
            >>> metadata = {"machine": "ThetaGPU", "n_nodes": 4, "num_gpus_per_node": 8}
            >>> dbm.add("path/to/search/log_dir/", label="exp_101", description="The experiment 101", metadata=metadata)

        Args:
            log_dir (str): the path to the search's logging directory.
            label (str, optional): the label wished for the experiment. Defaults to None.
            description (str, optional): the description wished for the experiment. Defaults to None.
            pip_versions (str or bool, optional): a boolean for which ``False`` means that we don't store any pip version checkpoint, and ``True`` that we store the current pip version checkpoint ; or the path to a ``.json`` file corresponding to the ouptut of ``pip list --format json``. Defaults to True.
            metadata (dict, optional): a dictionary of metadata. When the same key is found in the default `default_metadata` and the passed `metadata` then the values from `default_metadata` are overriden by `metadata` values. Defaults to None.
        """
        context_path = os.path.join(log_dir, "context.yaml")
        with open(context_path, "r") as file:
            context = yaml.load(file, Loader=yaml.SafeLoader)

        results_path = os.path.join(log_dir, "results.csv")
        results = pd.read_csv(results_path).to_dict(orient="list")

        logs = []
        for log_file in context.get("logs", []):
            logging_path = os.path.join(log_dir, log_file)
            logs.append(open(logging_path, "r"))

        experiment = {
            "metadata": {
                "label": label,
                "description": description,
                "user": self._user_name,
                "add_date": str(datetime.now()),
                "env": self._get_pip_env(pip_versions=pip_versions),
                "search": context.get("search", None),
                **metadata,
            },
            "data": {
                "search": {
                    "calls": context.get("calls", None),
                    "results": results,
                },
                "logging": logs,  # TODO: how to save files as str ?
            },
        }
        self._db.insert(experiment)

    def get(self, id):
        """Returns the desired experiment from the database.

        Example Usage:

            >>> dbm = DBManager(user_name="Bob", path="path/to/db.json")
            >>> dbm.get(23)

        Args:
            id (int): index of the record to delete.
        """
        exp = self._db.get(doc_id=id)
        if exp is not None:
            exp = dict(exp)
            exp["id"] = id
        return exp

    def delete(self, ids: list):
        """Deletes an experiment from the database.

        Example Usage:

            >>> dbm = DBManager(user_name="Bob", path="path/to/db.json")
            >>> dbm.delete([23, 16])

        Args:
            ids (list): indexes of the records to delete.
        """
        self._db.remove(doc_ids=ids)

    def list(self):
        """Returns an iterator over the records stored in the database.

        Example Usage:

            >>> dbm = DBManager(user_name="Bob", path="path/to/db.json")
            >>> experiments = dbm.list()
            >>> for exp in experiments:
            >>>     ...
        """
        for exp in self._db:
            doc_id = exp.doc_id
            exp = dict(exp)
            exp["id"] = doc_id
            yield (exp)
