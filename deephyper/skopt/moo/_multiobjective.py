import abc
from unicodedata import is_normalized
import numpy as np
from deephyper.skopt.utils import is_listlike


class MoScalarFunction:
    """Abstract class representing a scalarizing function.

    Args:
        n_objectives (int, optional): Number of objective functions to be scalarized. Defaults to 1.
        utopia_point (float or 1-D array, optional): Array of reference values for each objective function. Defaults to None.
        random_state (int, optional): Random seed. Defaults to None.
    """

    def __init__(
        self,
        n_objectives: int = 1,
        utopia_point=None,
        random_state=None,
    ):
        self._seed = None
        if type(random_state) is int:
            self._seed = random_state
            self._rng = np.random.RandomState(random_state)
        elif isinstance(random_state, np.random.RandomState):
            self._rng = random_state
        else:
            self._rng = np.random.RandomState()

        if not (type(n_objectives) is int):
            raise ValueError("Parameter 'n_objectives' shoud be an integer value!")
        self._n_objectives = n_objectives

        self._utopia_point = None
        if utopia_point is not None:
            self._check_shape(utopia_point)
            self._utopia_point = np.asarray(utopia_point)
        self._scaling = np.ones(self._n_objectives)

    def _check_shape(self, y):
        """Check if the shape of y is consistent with the object."""
        if not (
            (np.ndim(y) == 0 and self._n_objectives == 1)
            or (np.ndim(y) == 1 and np.shape(y)[0] == self._n_objectives)
        ):
            raise ValueError(
                f"expected y to be a scalar or 1-D array of length {self._n_objectives}"
            )

    def scalarize(self, y):
        """Convert the input array (or scalar) into a scalar value.

        Args:
            yi (scalar or 1-D array): The input array or scalar to be scalarized.

        Returns:
            float: The converted scalar value.
        """
        self._check_shape(y)
        if np.ndim(y) == 0:
            return y
        return self._scalarize(y)

    def normalize(self, yi):
        """Compute normalization constants based on the history of evaluated objective values.

        Args:
            yi (array): Array of evaluated objective values.

        Raises:
            ValueError: Raised if yi is not a list of scalars each of length _n_objectives.
        """
        if not is_listlike(yi):
            raise ValueError(f"expected yi to be a list")
        for y in yi:
            self._check_shape(y)
        y_max = np.max(yi, axis=0)
        y_min = np.min(yi, axis=0)
        self._utopia_point = y_min
        self._scaling = 1.0 / np.maximum(y_max - y_min, 1e-6)

    @abc.abstractmethod
    def _scalarize(self, yi):
        """Scalarization to be implemented.

        Args:
            yi: Array of length _n_objectives.

        Returns:
            float: Converted scalar value.
        """


class MoLinearFunction(MoScalarFunction):
    def __init__(
        self,
        n_objectives: int = 1,
        utopia_point=None,
        random_state=None,
    ):
        super().__init__(n_objectives, utopia_point, random_state)
        self._weight = self._rng.rand(self._n_objectives)
        self._weight /= np.sum(self._weight)

    def _scalarize(self, yi):
        return np.dot(self._weight, np.asarray(yi))


class MoChebyshevFunction(MoScalarFunction):
    def __init__(
        self,
        n_objectives: int = 1,
        utopia_point=None,
        random_state=None,
    ):
        super().__init__(n_objectives, utopia_point, random_state)
        self._weight = self._rng.rand(self._n_objectives)
        # self._weight = 0.5 * np.ones(self._n_objectives)
        # self._weight /= np.sum(self._weight)

    def _scalarize(self, yi):
        yi = np.dot(self._scaling, np.asarray(yi) - self._utopia_point)
        return np.max(self._weight * np.abs(yi))


class MoPBIFunction(MoScalarFunction):
    def __init__(
        self,
        n_objectives: int = 1,
        utopia_point=None,
        random_state=None,
        penalty: float = 100.0,
    ):
        super().__init__(n_objectives, utopia_point, random_state)
        self._weight = self._rng.rand(self._n_objectives)
        self._weightnorm = np.linalg.norm(self._weight) ** 2
        # self._weight /= np.sum(self._weight)
        self._penalty = np.abs(penalty) if np.isreal(penalty) else 100.0

    def _scalarize(self, yi):
        yi = np.dot(self._scaling, np.asarray(yi) - self._utopia_point)
        d1 = np.dot(self._weight, yi) / self._weightnorm
        d2 = np.linalg.norm(yi - (d1 * self._weight), 1)
        return d1 + (self._penalty * d2)
